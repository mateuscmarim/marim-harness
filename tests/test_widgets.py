import pytest
from pydantic_ai.usage import RunUsage
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Markdown

from marim_harness.interfaces.tui.widgets import (
    AssistantMessage,
    PromptInput,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessage,
    _reverse_edits,
    compute_diff_rows,
    format_cost,
    format_token_split,
    render_edit_diff,
    render_file_diff,
    strip_line_numbers,
)


def test_format_cost_uses_more_precision_for_sub_cent_amounts():
    assert format_cost(0.0042) == "$0.0042"
    assert format_cost(0.07) == "$0.07"
    assert format_cost(1.5) == "$1.50"


def test_format_token_split_uses_compact_symbols():
    # ↑ uncached input, ⚡ cached (read + write), ↓ output.
    u = RunUsage(
        input_tokens=56000, output_tokens=2000,
        cache_read_tokens=50000, cache_write_tokens=5000,
    )
    assert format_token_split(u) == "1k↑ 55k⚡ 2k↓"


def test_format_token_split_keeps_all_buckets_even_when_zero():
    # Stable layout: zero cache/output still render so the bar doesn't reflow.
    u = RunUsage(input_tokens=12, output_tokens=8)
    assert format_token_split(u) == "12↑ 0⚡ 8↓"

# An unclosed, expression-style bracket sequence. Unlike a balanced ``[/]``,
# ``rich``/``textual`` ``escape()`` will NOT neutralise this — its regex only
# escapes brackets that look like a complete ``[tag]`` — yet Textual's markup
# parser still treats ``[edit(`` as an opening tag and crashes on the dangling
# quote with "Expected markup value". Untrusted text must therefore bypass
# markup parsing entirely (literal Content), not merely be escaped.
MARKUP_BOMB = "[/] and [edit(old_string=\"unterminated"


class _Harness(App):
    def compose(self) -> ComposeResult:
        # read_file renders generically (collapsed, arg-repr body) — edit_file now
        # has special diff rendering covered by the diff tests below.
        yield ToolCallWidget("read_file", {"path": "a.txt"})


@pytest.mark.anyio
async def test_tool_widget_starts_pending_and_collapsed():
    app = _Harness()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        assert w.status == "pending"
        assert w.collapsed is True
        await pilot.pause()


@pytest.mark.anyio
async def test_tool_widget_finish_updates_status():
    app = _Harness()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("edited a.txt")
        await pilot.pause()
        assert w.status == "done"
        assert w.result_text == "edited a.txt"


@pytest.mark.anyio
async def test_tool_widget_is_collapsible_with_working_title():
    """The widget must NOT override compose(); it must keep Collapsible's
    title bar (the thing you click to expand) intact."""
    app = _Harness()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        await pilot.pause()
        assert isinstance(w, Collapsible)
        # Collapsible builds its own title bar; ours must still exist.
        assert w.query_one(Collapsible.Contents) is not None
        # The summary glyph shows the pending state in the title.
        assert "read_file" in str(w.title)
        assert "·" in str(w.title)


class _GroupHarness(App):
    def compose(self) -> ComposeResult:
        yield ToolGroupWidget()


@pytest.mark.anyio
async def test_tool_group_starts_collapsed():
    """A group only ever holds a burst (2+ calls), so it's born collapsed — a lone
    call is left bare by the caller and never reaches a group."""
    app = _GroupHarness()
    async with app.run_test() as pilot:
        g = app.query_one(ToolGroupWidget)
        await pilot.pause()
        assert g.collapsed is True


@pytest.mark.anyio
async def test_tool_group_summarizes_a_burst():
    """Two-or-more consecutive calls fold to one line; the title summarizes the
    batch (total + per-tool breakdown) and stays collapsed."""
    app = _GroupHarness()
    async with app.run_test() as pilot:
        g = app.query_one(ToolGroupWidget)
        await g.add_tool(ToolCallWidget("read_file", {"path": "a.py"}))
        await g.add_tool(ToolCallWidget("read_file", {"path": "b.py"}))
        await g.add_tool(ToolCallWidget("grep", {"pattern": "x"}))
        await pilot.pause()
        assert g.collapsed is True
        title = str(g.title)
        assert "3 tools" in title
        assert "read_file ×2" in title
        assert "grep" in title
        assert len(g.query(ToolCallWidget)) == 3


@pytest.mark.anyio
async def test_tool_group_is_collapsible():
    app = _GroupHarness()
    async with app.run_test() as pilot:
        g = app.query_one(ToolGroupWidget)
        await pilot.pause()
        assert isinstance(g, Collapsible)


@pytest.mark.anyio
async def test_tool_widget_body_shows_args_and_result():
    app = _Harness()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("done editing")
        await pilot.pause()
        body = str(w.query_one("#tool-body").render())
        assert "a.txt" in body
        assert "done editing" in body
        assert "✓" in str(w.title)  # done glyph


def test_strip_line_numbers():
    raw = "1\tdef greet():\n2\t    return 1\n3\t"
    assert strip_line_numbers(raw) == "def greet():\n    return 1\n"


def test_strip_line_numbers_leaves_plain_text_alone():
    raw = "just some text\nwith no prefixes"
    assert strip_line_numbers(raw) == raw


@pytest.mark.anyio
async def test_read_file_result_is_syntax_highlighted():
    """A read_file result should render syntax-highlighted (a styled Text), not raw,
    with the line-number prefixes stripped."""

    class H(App):
        def compose(self) -> ComposeResult:
            yield ToolCallWidget("read_file", {"path": "app.py"})

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("1\tdef greet():\n2\t    return 1\n")
        await pilot.pause()
        from rich.text import Text

        result = w._result_renderable()
        assert isinstance(result, Text)  # highlighted into a Text, not a raw str
        assert result.spans  # actually carries syntax-highlight styling
        assert "def greet" in result.plain  # content kept, line numbers stripped


@pytest.mark.anyio
async def test_user_message_has_user_class():
    class H(App):
        def compose(self) -> ComposeResult:
            yield UserMessage("hi there")

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(UserMessage)
        await pilot.pause()
        assert w.has_class("user-msg")
        assert "hi there" in str(w.render())


@pytest.mark.anyio
async def test_error_message_has_error_class_and_text():
    from marim_harness.interfaces.tui.widgets import ErrorMessage

    class H(App):
        def compose(self) -> ComposeResult:
            yield ErrorMessage("rate limited (429)")

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(ErrorMessage)
        await pilot.pause()
        assert w.has_class("error-msg")
        assert "rate limited (429)" in str(w.render())


@pytest.mark.anyio
async def test_log_messages_survive_markup_like_text():
    """Error/user/notice text is arbitrary (exceptions, user input, MCP errors)
    and may contain Rich markup syntax like ``[/]``. It must be shown literally,
    never parsed as markup — otherwise a MarkupError crashes the whole app."""
    from marim_harness.interfaces.tui.widgets import ErrorMessage, NoticeMessage

    payload = "MarkupError: auto closing tag ('[/]') has nothing to close " + MARKUP_BOMB

    class H(App):
        def compose(self) -> ComposeResult:
            yield ErrorMessage(payload)
            yield UserMessage(payload)
            yield NoticeMessage(payload)

    app = H()
    async with app.run_test() as pilot:
        # Force a real layout/render pass — this is where markup is parsed.
        await pilot.pause()
        for cls in (ErrorMessage, UserMessage, NoticeMessage):
            w = app.query_one(cls)
            assert "[/]" in str(w.render())


@pytest.mark.anyio
async def test_tool_widget_survives_markup_like_args_and_result():
    """Tool args and results are arbitrary (commands, file content, output) and
    may contain Rich markup syntax like ``[/]``. Neither the title nor the body
    may parse it as markup — otherwise a MarkupError crashes the turn."""
    payload = "grep -n '[/]' file && echo [done] " + MARKUP_BOMB

    class H(App):
        def compose(self) -> ComposeResult:
            yield ToolCallWidget("bash", {"command": payload})

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("matched [/] on line 3 [reset] " + MARKUP_BOMB)
        await pilot.pause()
        body = str(w.query_one("#tool-body").render())
        assert "[/]" in body


@pytest.mark.anyio
async def test_subagent_widget_survives_markup_like_task():
    """A spawned sub-agent's task text is arbitrary and may contain markup
    syntax; the card header must render it literally rather than crash."""
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    class H(App):
        def compose(self) -> ComposeResult:
            yield SubAgentWidget("Explore", MARKUP_BOMB)

    app = H()
    async with app.run_test() as pilot:
        await pilot.pause()
        w = app.query_one(SubAgentWidget)
        assert "[/]" in str(w._header.visual)


@pytest.mark.anyio
async def test_task_and_job_panels_survive_markup_like_text():
    """Task text and job labels are untrusted and may contain markup syntax;
    the panels must render them literally rather than crash."""
    from marim_harness.interfaces.tui.widgets import JobPanel, TaskPanel

    class _Task:
        status = "pending"
        text = "fix the [/] bug " + MARKUP_BOMB

    class _Job:
        id = "job-1"
        kind = "agent"
        status = "running"
        label = "render the [/] panel " + MARKUP_BOMB

    class H(App):
        def compose(self) -> ComposeResult:
            yield TaskPanel()
            yield JobPanel()

    app = H()
    async with app.run_test() as pilot:
        app.query_one(TaskPanel).show_tasks([_Task()])
        app.query_one(JobPanel).show_jobs([_Job()])
        await pilot.pause()
        assert "[/]" in str(app.query_one("#task-body").render())
        assert "[/]" in str(app.query_one("#job-body").render())


def _panel_item(kind):
    """A duck-typed task or job item the panel renderers accept."""
    if kind == "task":
        class _Task:
            status = "pending"
            text = "build the thing"
        return _Task()

    class _Job:
        id = "job-1"
        kind = "agent"
        status = "running"
        label = "build the thing"
    return _Job()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["task", "job"])
async def test_live_panel_keeps_title_visible_and_toggles_when_collapsed(kind):
    """Collapsing a panel must keep its sticky title on screen (so the user
    still sees "Tasks"/"Jobs") and leave a clickable target to expand again.
    The docked header contributes nothing to the panel's auto-height, so without
    a floor the whole panel shrank to zero rows on collapse — title gone, nothing
    left to click. The fix lives in the shared LivePanel, so both panels are
    guarded here."""
    from marim_harness.interfaces.tui.widgets import JobPanel, TaskPanel
    from marim_harness.interfaces.tui.widgets.panels import PanelHeader

    panel_cls = TaskPanel if kind == "task" else JobPanel
    items = [_panel_item(kind) for _ in range(5)]

    class H(App):
        def compose(self) -> ComposeResult:
            yield panel_cls()

    app = H()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one(panel_cls)
        panel._render_items(items)
        await pilot.pause()
        assert panel.size.height > 1  # expanded shows header + body

        panel.on_panel_header_clicked(PanelHeader.Clicked())
        await pilot.pause()
        # Collapsed: body hidden, but the title row must survive on screen so
        # the user still sees the title and has something to click to expand.
        assert panel._body.display is False
        assert panel.size.height >= 1, "collapsed panel vanished — title gone, nothing to click"
        visible = app.screen._compositor.visible_widgets
        assert panel._header in visible, "collapsed title row not rendered"

        panel.on_panel_header_clicked(PanelHeader.Clicked())
        await pilot.pause()
        assert panel._body.display is True
        assert panel.size.height > 1  # expands back


class _PromptHost(App):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptInput()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self.submitted.append(event.value)


@pytest.mark.anyio
async def test_enter_submits_typed_text():
    app = _PromptHost()
    async with app.run_test() as pilot:
        app.query_one(PromptInput).focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["hi"]


@pytest.mark.anyio
async def test_shift_enter_inserts_newline_without_submitting():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("shift+enter")
        await pilot.press("b")
        await pilot.pause()
        assert pi.text == "a\nb"
        assert app.submitted == []  # no submit fired


@pytest.mark.anyio
async def test_ctrl_j_inserts_newline_without_submitting():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("ctrl+j")
        await pilot.press("b")
        await pilot.pause()
        assert pi.text == "a\nb"
        assert app.submitted == []


@pytest.mark.anyio
async def test_target_height_grows_and_caps():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        await pilot.pause()
        pi.text = ""
        assert pi._target_height() == PromptInput._MIN_LINES  # empty holds the floor
        pi.text = "a\nb\nc"
        assert pi._target_height() == PromptInput._MIN_LINES  # within the floor
        pi.text = "\n".join(str(i) for i in range(5))
        assert pi._target_height() == 5  # grows with logical lines
        pi.text = "\n".join(str(i) for i in range(20))
        assert pi._target_height() == PromptInput._MAX_LINES  # capped


@pytest.mark.anyio
async def test_assistant_message_is_markdown_and_accumulates():
    class H(App):
        def compose(self) -> ComposeResult:
            yield AssistantMessage()

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(AssistantMessage)
        assert isinstance(w, Markdown)
        w.append("# Title")
        w.append(" more")
        await pilot.pause()
        assert w.text == "# Title more"


@pytest.mark.anyio
async def test_assistant_message_defers_render_until_flush():
    """append() buffers text without re-parsing the markdown; flush() does the one
    (expensive) render. This is the per-delta debounce on the streaming hot path."""

    class H(App):
        def compose(self) -> ComposeResult:
            yield AssistantMessage()

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(AssistantMessage)
        await pilot.pause()
        w.flush()  # clear the initial mount state
        w.append("# Title")
        w.append(" more")
        # Text accumulates immediately, but the render is deferred.
        assert w.text == "# Title more"
        assert w._pending is True
        # Flushing performs the render and clears the pending flag.
        assert w.flush() is True
        assert w._pending is False
        # A flush with nothing buffered is a no-op.
        assert w.flush() is False


def _history_host(hist):
    class H(App):
        def compose(self) -> ComposeResult:
            yield PromptInput(history=hist)

    return H()


@pytest.mark.anyio
async def test_prompt_input_up_recalls_previous_entries():
    from marim_harness.history import PromptHistory

    hist = PromptHistory()
    for p in ("one", "two", "three"):
        hist.add(p)

    app = _history_host(hist)
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("up")
        assert pi.text == "three"  # newest first
        await pilot.press("up")
        assert pi.text == "two"
        await pilot.press("up")
        assert pi.text == "one"
        await pilot.press("up")
        assert pi.text == "one"  # stops at the oldest


@pytest.mark.anyio
async def test_prompt_input_down_restores_in_progress_draft():
    from marim_harness.history import PromptHistory

    hist = PromptHistory()
    hist.add("one")
    hist.add("two")

    app = _history_host(hist)
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.text = "draft"  # something in progress
        await pilot.press("up")
        assert pi.text == "two"
        await pilot.press("up")
        assert pi.text == "one"
        await pilot.press("down")
        assert pi.text == "two"
        await pilot.press("down")
        assert pi.text == "draft"  # past the newest -> the draft comes back


@pytest.mark.anyio
async def test_prompt_input_arrows_move_within_multiline_before_history():
    """Up only recalls history at the first line; inside a multi-line draft the
    arrows move the cursor normally and leave the text untouched."""
    from marim_harness.history import PromptHistory

    hist = PromptHistory()
    hist.add("recalled")

    app = _history_host(hist)
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.text = "a\nb\nc"
        pi.move_cursor(pi.document.end)  # cursor on the last line
        await pilot.press("up")
        assert pi.text == "a\nb\nc"  # moved cursor, did not recall
        await pilot.press("up")
        assert pi.text == "a\nb\nc"  # now on the first line, still the draft
        await pilot.press("up")
        assert pi.text == "recalled"  # at the boundary -> history kicks in


@pytest.mark.anyio
async def test_prompt_input_submit_resets_navigation():
    from marim_harness.history import PromptHistory

    hist = PromptHistory()
    hist.add("one")
    hist.add("two")

    app = _history_host(hist)
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("up")
        assert pi.text == "two"
        assert pi._hist_idx is not None  # navigating
        await pilot.press("enter")  # submit
        assert pi._hist_idx is None  # navigation reset for the next line


class _SubHarness(App):
    def compose(self) -> ComposeResult:
        from marim_harness.interfaces.tui.widgets import SubAgentWidget

        yield SubAgentWidget("explore", "map the code")


def test_failure_reason_strips_prefix_and_clips():
    from marim_harness.interfaces.tui.widgets.subagent import failure_reason

    # The "Sub-agent 'x' failed: " prefix is stripped, leaving the real error.
    assert failure_reason("Sub-agent 'explore' failed: ValueError: boom") == "ValueError: boom"
    # Other failure messages pass through (whitespace collapsed).
    assert failure_reason("No sub-agent type 'ghost'.\nAvailable: explore") == (
        "No sub-agent type 'ghost'. Available: explore"
    )
    assert failure_reason("Sub-agent 'x' failed: " + "y" * 200).endswith("…")


def test_derive_subagent_title_takes_first_clause():
    """A verbose spawn prompt condenses to its first sentence/clause as the title,
    instead of inlining the whole prompt."""
    from marim_harness.interfaces.tui.widgets.subagent import derive_title

    assert derive_title(
        "Provide a structural overview of the codebase. Include: a tree."
    ) == "Provide a structural overview of the codebase"
    # No boundary → the whole (whitespace-collapsed) task; multi-line is flattened.
    assert derive_title("short\n  task") == "short task"
    # Over-long single clause is clipped with an ellipsis.
    assert derive_title("x" * 200).endswith("…")


@pytest.mark.anyio
async def test_subagent_card_hover_toggles_highlight_class():
    """Hovering anywhere on the card adds the `-hovered` class (which the CSS uses to
    brighten the whole row); it persists while moving between the card's two lines
    and clears on leave. The class is needed because CSS :hover only lands on the
    leaf line under the pointer, not the container."""
    from textual.containers import VerticalScroll

    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    class H(App):
        def compose(self) -> ComposeResult:
            yield VerticalScroll(id="log")

        async def on_mount(self) -> None:
            self.card = SubAgentWidget("explore", "map the code. then report.", "m1")
            self.other = SubAgentWidget("explore", "another task. do it.", "m2")
            await self.query_one("#log").mount(self.card, self.other)

    app = H()
    async with app.run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        card, other = app.card, app.other
        assert card.has_class("-hovered") is False
        await pilot.hover(card, offset=(2, 0))  # hover the card's header line
        await pilot.pause()
        assert card.has_class("-hovered") is True
        # Hopping to the ↳ line keeps the highlight (no flicker).
        await pilot.hover(card, offset=(2, 1))
        await pilot.pause()
        assert card.has_class("-hovered") is True
        # Moving onto the other card clears the first and lights the second.
        await pilot.hover(other, offset=(2, 0))
        await pilot.pause()
        await pilot.pause()
        assert card.has_class("-hovered") is False
        assert other.has_class("-hovered") is True


@pytest.mark.anyio
async def test_subagent_card_body_hidden_inline():
    """The transcript home is mounted but hidden inline — it's only revealed by the
    full-screen viewer, so the inline log shows a compact card."""
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(SubAgentWidget).body.display is False


@pytest.mark.anyio
async def test_subagent_card_shows_current_tool_then_tally():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        # While running, the ↳ line shows the current (humanized) tool + its target.
        w.note_tool("read_file", "src/foo.py")
        assert "Read src/foo.py" in str(w._activity.visual)
        w.note_tool("grep", "needle")
        assert "Grep needle" in str(w._activity.visual)
        # Once finished, it collapses to the run summary (tally + frozen duration).
        w.finish("all done", status="done")
        assert "2 toolcalls" in str(w._activity.visual)


@pytest.mark.anyio
async def test_subagent_finish_marks_done_and_freezes_duration():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.note_tool("grep")
        w.finish("all done", status="done")
        # Once finished, the header shows ✓, the summary uses a singular toolcall,
        # and the duration is frozen.
        assert "✓" in str(w._header.visual)
        assert "1 toolcall " in str(w._activity.visual)
        assert w._t_end is not None


@pytest.mark.anyio
async def test_subagent_tracks_token_usage():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        assert w.tokens == 0
        w.set_tokens(1500)
        assert w.tokens == 1500
        # Recording activity doesn't clobber the running token total.
        w.note_tool("grep")
        assert w.tokens == 1500


@pytest.mark.anyio
async def test_subagent_token_usage_survives_finish():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.set_tokens(2400)
        w.finish("all done", status="done")
        # The final token count stays available (for the viewer footer).
        assert w.tokens == 2400


@pytest.mark.anyio
async def test_subagent_set_usage_stores_total_cost_and_split():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.set_usage(1500, "$0.03", "1k↑ 0⚡ 500↓")
        assert w.tokens == 1500
        assert w.cost_text == "$0.03"
        assert w.split_text == "1k↑ 0⚡ 500↓"


@pytest.mark.anyio
async def test_subagent_expanded_body_shows_full_split_and_cost():
    from textual.widgets import Static

    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.set_usage(56000, "$0.12", "1k↑ 55k⚡ 2k↓")
        await pilot.pause()
        # The detailed split + cost live in the (expanded) body, where there's
        # room — mirroring the session status bar.
        usage_line = w.body.query_one(".subagent-usage", Static)
        text = str(usage_line.visual)
        assert "1k↑ 55k⚡ 2k↓" in text
        assert "$0.12" in text


@pytest.mark.anyio
async def test_subagent_body_usage_omits_cost_when_unpriced():
    from textual.widgets import Static

    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        # An unpriced model yields no cost — the body usage line shows the split
        # only, no stray '$'.
        w.set_usage(1500, None, "1k↑ 0⚡ 500↓")
        await pilot.pause()
        text = str(w.body.query_one(".subagent-usage", Static).visual)
        assert "1k↑ 0⚡ 500↓" in text
        assert "$" not in text


def _plain(renderable) -> str:
    """Flatten a Text/Group/str renderable to its plain text for assertions."""
    if isinstance(renderable, Text):
        return renderable.plain
    parts = getattr(renderable, "renderables", None)
    if parts is not None:
        return "\n".join(_plain(p) for p in parts)
    return str(renderable)


def test_render_edit_diff_basic():
    text, added, removed = render_edit_diff(
        [{"old_string": "foo\nbar", "new_string": "baz"}], cap=None
    )
    plain = text.plain
    assert "- foo" in plain and "- bar" in plain
    assert "+ baz" in plain
    assert removed == 2 and added == 1


def test_render_edit_diff_multi_edit_counts():
    _, added, removed = render_edit_diff(
        [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "c", "new_string": "d\ne"},
        ],
        cap=None,
    )
    assert added == 3 and removed == 2  # b + d + e ; a + c


def test_render_edit_diff_caps_and_footers():
    edits = [{"old_string": "\n".join(f"o{i}" for i in range(30)), "new_string": "x"}]
    capped, _, _ = render_edit_diff(edits, cap=20)
    assert "more lines (ctrl+o)" in capped.plain
    assert len(capped.plain.splitlines()) <= 21  # ~20 lines + the footer
    full, _, _ = render_edit_diff(edits, cap=None)
    assert "more lines" not in full.plain
    assert len(full.plain.splitlines()) >= 30


def test_render_edit_diff_empty_and_malformed():
    text, a, r = render_edit_diff([], cap=None)
    assert text.plain == "" and a == 0 and r == 0
    text2, a2, r2 = render_edit_diff(["nope", {"new_string": "only add"}], cap=None)
    assert "+ only add" in text2.plain and r2 == 0 and a2 == 1


def _render_lines(renderable, width=80) -> list[str]:
    """Render a Rich renderable to plain text lines (styles stripped) for asserting
    on the visible diff text — line numbers, +/- markers, content."""
    import io

    from rich.console import Console

    con = Console(width=width, file=io.StringIO(), color_system=None)
    con.print(renderable)
    return con.file.getvalue().splitlines()


def test_compute_diff_rows_classifies_lines_with_real_numbers():
    rows, added, removed = compute_diff_rows("a\nb\nc\n", "a\nX\nc\n")
    kinds = [(r.kind, r.old_no, r.new_no, r.text) for r in rows]
    assert ("context", 1, 1, "a") in kinds
    assert ("remove", 2, None, "b") in kinds
    assert ("add", None, 2, "X") in kinds
    assert ("context", 3, 3, "c") in kinds
    assert added == 1 and removed == 1


def test_compute_diff_rows_gaps_between_separated_hunks():
    old = "\n".join(str(i) for i in range(1, 21))
    new = old.replace("2", "TWO").replace("19", "NINETEEN")
    rows, _, _ = compute_diff_rows(old, new, context=2)
    assert any(r.kind == "gap" for r in rows)  # the unchanged middle is collapsed


def test_highlight_lines_styles_newline_terminated_source():
    # A newline-terminated file: str.split keeps a trailing "" that Rich's split
    # drops — the rows must still align AND keep syntax styling (regression: the
    # count mismatch used to silently fall back to unstyled plain text).
    from marim_harness.interfaces.tui.widgets import _highlight_lines

    lines = _highlight_lines("def foo(x):\n    return x + 1\n", "python")
    assert len(lines) == 3  # "def…", "    return…", ""
    assert any(line.spans for line in lines)  # actually syntax-highlighted


def test_render_file_diff_shows_numbers_markers_and_content():
    diff, added, removed = render_file_diff("a\nb\nc\n", "a\nX\nc\n", cap=None)
    text = "\n".join(_render_lines(diff))
    assert "- b" in text or "-  b" in text or "- " in text and "b" in text
    assert "+ X" in text or "X" in text
    assert "2" in text  # a real line number in the gutter
    assert added == 1 and removed == 1


def test_summary_widget_is_collapsed_and_shows_body():
    from textual.widgets import Collapsible

    from marim_harness.interfaces.tui.widgets import SummaryWidget

    w = SummaryWidget("We fixed the parser and added tests.")
    assert isinstance(w, Collapsible)
    assert w.collapsed is True  # unobtrusive; click to read
    assert "Conversation summary" in str(w.title)
    body = str(w._body.render())
    assert "We fixed the parser and added tests." in body
    assert "Summary of earlier conversation" not in body  # prefix already stripped


def test_render_file_diff_context_lines_have_no_background():
    # Context (unchanged) lines must carry NO background so they inherit the
    # widget's themed background. Rich's Syntax bakes a "default" bg into every
    # token; left unstripped it renders as the terminal default (often black),
    # producing an ugly text-width dark box on context rows.
    import io

    from rich.console import Console

    diff, _, _ = render_file_diff("a = 1\nb = 2\nc = 3\n", "a = 1\nb = 9\nc = 3\n",
                                  cap=None, lexer="python")
    con = Console(width=40, color_system="truecolor", file=io.StringIO())
    opts = con.options.update_width(40)
    rows = con.render_lines(diff, opts)
    # Row 0 is context ("a = 1"); none of its segments may set a background.
    bgs = [s.style.bgcolor for s in rows[0] if s.style and s.style.bgcolor is not None]
    assert bgs == [], f"context line should have no bg, got {bgs}"


def test_render_file_diff_caps_and_reveals():
    old = "\n".join(f"o{i}" for i in range(40))
    new = "\n".join(f"n{i}" for i in range(40))
    capped, _, _ = render_file_diff(old, new, cap=20)
    assert "more lines (ctrl+o)" in "\n".join(_render_lines(capped))
    full, _, _ = render_file_diff(old, new, cap=None)
    assert "more lines" not in "\n".join(_render_lines(full))


def test_reverse_edits_reconstructs_and_verifies():
    new_text = "hello world\nsecond line\n"
    edits = [{"old_string": "planet", "new_string": "world"}]
    # The pre-edit file had "planet" where "world" now is.
    assert _reverse_edits(new_text, edits) == "hello planet\nsecond line\n"


def test_reverse_edits_returns_none_when_unverifiable():
    # "ab" appears twice in new_text, so reverse-replacing the first one yields a
    # text where old_string "a" no longer matches uniquely — the forward re-check
    # fails and we bail to the simple diff rather than show a wrong reconstruction.
    new_text = "ab\nab\n"
    edits = [{"old_string": "a", "new_string": "ab"}]
    assert _reverse_edits(new_text, edits) is None


def test_edit_file_widget_uses_file_diff_when_text_loaded():
    w = ToolCallWidget(
        "edit_file", {"path": "a.py", "edits": [{"old_string": "b", "new_string": "X"}]}
    )
    w._old_text = "a\nb\nc\n"
    w._new_text = "a\nX\nc\n"
    body = "\n".join(_render_lines(w._render_body()))
    assert "X" in body and "b" in body
    assert "2" in body  # gutter line numbers, the file-diff hallmark


class _EditHarness(App):
    """Mounts a single edit_file widget; the diff tests drive finish() on it."""

    def __init__(self, args: dict, workspace_root=None) -> None:
        self._args = args
        self._workspace_root = workspace_root
        super().__init__()

    def compose(self) -> ComposeResult:
        yield ToolCallWidget(
            "edit_file", self._args, workspace_root=self._workspace_root
        )


@pytest.mark.anyio
async def test_finish_loads_real_file_and_renders_gutter_diff(tmp_path):
    """The real path: finish() reads the post-edit file via the injected workspace
    root, reconstructs the pre-edit text, and renders a gutter/line-number diff.
    Exercises _load_diff → resolve_in_workspace → read_text → _reverse_edits end to
    end — the chain every other diff test stubs past."""
    (tmp_path / "g.py").write_text("a\nX\nc\n")  # post-edit content on disk
    app = _EditHarness(
        {"path": "g.py", "edits": [{"old_string": "b", "new_string": "X"}]},
        workspace_root=tmp_path,
    )
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("edited g.py (1 edit)")
        await pilot.pause()
        assert w._old_text == "a\nb\nc\n"  # reconstructed pre-edit text
        body = "\n".join(_render_lines(w._render_body()))
        assert "1 " in body and "2 " in body  # real gutter line numbers
        assert "b" in body and "X" in body  # the removed/added content


@pytest.mark.anyio
async def test_finish_falls_back_when_file_unreadable(tmp_path):
    """If the file can't be read (missing/changed), finish() leaves the simple
    diff in place instead of crashing or blanking — the swallowed-error path."""
    app = _EditHarness(
        {"path": "gone.py", "edits": [{"old_string": "b", "new_string": "X"}]},
        workspace_root=tmp_path,
    )
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("edited gone.py")  # file does not exist
        await pilot.pause()
        assert w._old_text is None  # no reconstruction
        assert "+ X" in _plain(w._render_body())  # simple diff still shows


def test_edit_file_widget_renders_diff_and_is_expanded():
    w = ToolCallWidget(
        "edit_file", {"path": "a.py", "edits": [{"old_string": "x", "new_string": "y"}]}
    )
    assert w.collapsed is False  # auto-expanded inline
    body = _plain(w._render_body())
    assert "- x" in body and "+ y" in body
    assert "edits=[" not in body  # not the raw repr
    title = str(w.title)
    assert "a.py" in title and "+1" in title and "1" in title  # path + stat


def test_edit_file_diff_caps_until_revealed():
    edits = [{"old_string": "\n".join(f"o{i}" for i in range(40)), "new_string": "z"}]
    w = ToolCallWidget("edit_file", {"path": "a.py", "edits": edits})
    assert "more lines (ctrl+o)" in _plain(w._render_body())  # capped by default
    w.reveal = True  # what set_reveal flips (the mounted path is covered in test_app)
    assert "more lines" not in _plain(w._render_body())  # uncapped when revealed


def test_read_file_highlight_has_no_baked_background():
    # read_file content is syntax-highlighted; like the diff, it must not carry the
    # baked "default" background (which renders as a stray dark box) — it should
    # inherit the widget background.
    import io

    from rich.console import Console

    w = ToolCallWidget("read_file", {"path": "a.py"})
    w.finish("1\tdef f(x):\n2\t    return x + 1\n")
    con = Console(width=60, color_system="truecolor", file=io.StringIO())
    lines = con.render_lines(w._render_body(), con.options.update_width(60))
    bgs = [s.style.bgcolor for line in lines for s in line
           if s.style and s.style.bgcolor is not None]
    assert bgs == [], f"highlighted code should have no background, got {bgs}"
    text = "\n".join("".join(s.text for s in line) for line in lines)
    assert "def f" in text  # still rendered (and highlighted)


def test_write_file_widget_highlights_content():
    w = ToolCallWidget("write_file", {"path": "a.py", "content": "x = 1\n"})
    body = w._render_body()
    assert isinstance(body, Syntax) or "x = 1" in _plain(body)
    assert "content=" not in _plain(body)  # not the raw arg repr


def test_non_special_tool_stays_collapsed():
    w = ToolCallWidget("bash", {"command": "ls"})
    assert w.collapsed is True


def test_single_arg_tool_title_drops_key_and_quotes():
    # A one-arg tool reads as "tool · value" — no redundant key=, no repr quotes.
    w = ToolCallWidget("bash", {"command": "uv run pytest"})
    title = str(w.title)
    assert "bash · uv run pytest" in title
    assert "command=" not in title
    assert "'" not in title  # not the repr form


def test_multi_arg_tool_title_keeps_keyed_form():
    w = ToolCallWidget("read_file", {"path": "a.py", "offset": 515})
    title = str(w.title)
    assert "read_file(" in title
    assert "path=" in title and "offset=515" in title


def test_long_arg_preview_is_truncated():
    # A long command must not run off the title; it's clipped with an ellipsis.
    long_cmd = "git commit -m " + "x" * 200
    w = ToolCallWidget("bash", {"command": long_cmd})
    title = str(w.title)
    assert "…" in title
    assert len(title) < 140  # bounded, not the full 200+ chars


def test_preview_cap_allows_up_to_100_chars():
    value = "x" * 95  # under the 100-char cap
    w = ToolCallWidget("bash", {"command": value})
    title = str(w.title)
    assert value in title and "…" not in title


def test_bash_nonzero_exit_marks_failed_and_keeps_expanded():
    w = ToolCallWidget("bash", {"command": "false"})
    w.finish("exit 1\nsome error output")
    assert w.status == "failed"
    assert w.collapsed is False  # failures stay open
    assert "✗" in str(w.title)  # status indicator shows failure
    assert "✓" not in str(w.title)


def test_bash_zero_exit_stays_done_and_collapsed():
    w = ToolCallWidget("bash", {"command": "true"})
    w.finish("exit 0\nok")
    assert w.status == "done"
    assert w.collapsed is True
    assert "✓" in str(w.title)


def test_non_bash_exit_text_is_not_a_failure():
    # A non-bash tool whose output happens to contain "exit 1" must not be flagged.
    w = ToolCallWidget("read_file", {"path": "log.txt"})
    w.finish("1\texit 1 was logged here")
    assert w.status == "done"


def test_bash_failure_output_renders_red():
    import io

    from rich.console import Console

    w = ToolCallWidget("bash", {"command": "false"})
    w.finish("exit 1\nboom error here")
    con = Console(width=60, color_system="truecolor", file=io.StringIO())
    lines = con.render_lines(w._render_body(), con.options.update_width(60))
    reds = [s.text for line in lines for s in line
            if s.style and s.style.color and "d9544f" in str(s.style.color).lower()]
    assert any("boom" in t for t in reds)  # the output is colored red
