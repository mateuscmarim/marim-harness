import pytest
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Markdown

from marim_harness.interfaces.tui.widgets import (
    AssistantMessage,
    PromptInput,
    ToolCallWidget,
    UserMessage,
    strip_line_numbers,
)


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ToolCallWidget("edit_file", {"path": "a.txt"})


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
        assert "edit_file" in str(w.title)
        assert "·" in str(w.title)


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
    """A read_file tool result should render through rich Syntax, not raw."""

    class H(App):
        def compose(self) -> ComposeResult:
            yield ToolCallWidget("read_file", {"path": "app.py"})

    app = H()
    async with app.run_test() as pilot:
        w = app.query_one(ToolCallWidget)
        w.finish("1\tdef greet():\n2\t    return 1\n")
        await pilot.pause()
        from rich.syntax import Syntax

        # The body should hold a Syntax renderable for source files.
        assert isinstance(w._result_renderable(), Syntax)


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

    payload = "MarkupError: auto closing tag ('[/]') has nothing to close"

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


@pytest.mark.anyio
async def test_subagent_widget_collapsed_param():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    class H(App):
        def compose(self) -> ComposeResult:
            yield SubAgentWidget("explore", "t", collapsed=True)

    app = H()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(SubAgentWidget).collapsed is True


@pytest.mark.anyio
async def test_subagent_widget_default_expanded():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(SubAgentWidget).collapsed is False


@pytest.mark.anyio
async def test_subagent_title_shows_live_activity():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        # A tool call is reflected in the title with a running count.
        w.note_tool("grep")
        assert "grep" in str(w.title)
        assert "(1)" in str(w.title)
        w.note_tool("read_file")
        assert "(2)" in str(w.title)
        # Generating text shows a responding hint.
        w.note_text()
        assert "responding" in str(w.title)


@pytest.mark.anyio
async def test_subagent_finish_clears_activity_from_title():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.note_tool("grep")
        w.finish("all done", status="done")
        # Once finished, the title is the clean summary, no activity tail.
        assert "grep" not in str(w.title)
        assert "✓" in str(w.title)


@pytest.mark.anyio
async def test_subagent_title_shows_token_usage():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        # No tokens yet: no token tail in the title.
        assert "tok" not in str(w.title)
        # Once tokens are reported, the (collapsed-legible) title shows them.
        w.set_tokens(1500)
        assert "1.5k" in str(w.title)
        assert "tok" in str(w.title)
        # The count keeps the live activity alongside it.
        w.note_tool("grep")
        assert "grep" in str(w.title)
        assert "1.5k" in str(w.title)


@pytest.mark.anyio
async def test_subagent_token_usage_survives_finish():
    from marim_harness.interfaces.tui.widgets import SubAgentWidget

    app = _SubHarness()
    async with app.run_test() as pilot:
        w = app.query_one(SubAgentWidget)
        await pilot.pause()
        w.set_tokens(2400)
        w.finish("all done", status="done")
        # The final token count stays visible after the run finishes.
        assert "2.4k" in str(w.title)
        assert "tok" in str(w.title)
