"""Large pastes collapse to [Pasted text #N …] markers and expand at submit.
See docs/superpowers/specs/2026-07-02-paste-collapsing-design.md."""

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets.text_area import Selection

from marim_harness.interfaces.tui.widgets.prompt import PromptInput


class _PromptHost(App):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.steered: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptInput()

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self.submitted.append(event.value)

    def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
        self.steered.append(event.value)


async def _paste(pilot, pi: PromptInput, text: str) -> None:
    pi.post_message(events.Paste(text))
    await pilot.pause()


@pytest.mark.anyio
async def test_multiline_paste_collapses_to_marker():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"line {i}" for i in range(13))
        await _paste(pilot, pi, blob)
        assert pi.text == "[Pasted text #1 +13 lines]"
        assert pi.pastes == [blob]


@pytest.mark.anyio
async def test_long_single_line_paste_collapses_with_char_count():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "x" * 601
        await _paste(pilot, pi, blob)
        assert pi.text == "[Pasted text #1 +601 chars]"
        assert pi.pastes == [blob]


@pytest.mark.anyio
async def test_small_pastes_insert_normally():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await _paste(pilot, pi, "a\nb\nc")     # 3 lines: at the threshold, not over
        await _paste(pilot, pi, "y" * 600)      # 600 chars: at the threshold, not over
        assert pi.text == "a\nb\nc" + "y" * 600
        assert pi.pastes == []


@pytest.mark.anyio
async def test_submit_expands_markers_in_order():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        first = "\n".join(f"a{i}" for i in range(5))
        second = "\n".join(f"b{i}" for i in range(4))
        await _paste(pilot, pi, first)
        pi.insert(" between ")
        await _paste(pilot, pi, second)
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == [f"{first} between {second}"]
        assert pi.pastes == []  # stash cleared with the draft


@pytest.mark.anyio
async def test_steer_expands_markers_too():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"s{i}" for i in range(6))
        await _paste(pilot, pi, blob)
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert app.steered == [blob]
        assert pi.pastes == []
        # Steer consumes the draft, same as submit — a dead marker with no
        # backing stash entry must not survive for a later Enter to resubmit.
        assert pi.text == ""
        assert pi.attachments == []


@pytest.mark.anyio
async def test_unmatched_marker_submits_as_literal_text():
    """A hand-typed marker with no stash entry passes through unchanged."""
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.insert("[Pasted text #7 +9 lines]")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["[Pasted text #7 +9 lines]"]


@pytest.mark.anyio
async def test_small_paste_replaces_selection():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.insert("hello world")
        pi.selection = Selection((0, 0), (0, 5))
        await _paste(pilot, pi, "BYE")
        assert pi.text == "BYE world"


@pytest.mark.anyio
async def test_collapsing_paste_replaces_selection():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        pi.insert("hello world")
        pi.selection = Selection((0, 0), (0, 5))
        blob = "\n".join(f"line {i}" for i in range(13))
        await _paste(pilot, pi, blob)
        assert pi.text == "[Pasted text #1 +13 lines] world"
        assert pi.pastes == [blob]


@pytest.mark.anyio
async def test_backspace_removes_whole_marker_and_stash_entry():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        blob = "\n".join(f"line {i}" for i in range(13))
        await _paste(pilot, pi, blob)
        # Cursor sits right after the marker; one backspace kills all of it.
        await pilot.press("backspace")
        await pilot.pause()
        assert pi.text == ""
        assert pi.pastes == []


@pytest.mark.anyio
async def test_deleting_first_of_two_markers_renumbers_survivor():
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        first = "\n".join(f"a{i}" for i in range(5))
        second = "x" * 700
        await _paste(pilot, pi, first)
        await _paste(pilot, pi, second)
        assert pi.text == "[Pasted text #1 +5 lines][Pasted text #2 +700 chars]"
        # Put the cursor inside the FIRST marker and delete it.
        pi.move_cursor((0, 5))
        await pilot.press("backspace")
        await pilot.pause()
        # Survivor renumbers to #1 and keeps its own +chars tail.
        assert pi.text == "[Pasted text #1 +700 chars]"
        assert pi.pastes == [second]
        # And it still expands to the right content.
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == [second]


@pytest.mark.anyio
async def test_image_and_paste_markers_number_independently(tmp_path):
    """Pasting an image path makes [Image #1]; a text paste makes
    [Pasted text #1 …] — deleting the paste marker must not disturb the
    image attachment. Fake image bytes follow test_image_paste.py's pattern
    (there is no fixture file; a path ending .png with any bytes suffices)."""
    app = _PromptHost()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNGbytes")
        await _paste(pilot, pi, str(img))          # -> [Image #1]
        blob = "\n".join(f"l{i}" for i in range(9))
        await _paste(pilot, pi, blob)              # -> [Pasted text #1 +9 lines]
        assert pi.text == "[Image #1][Pasted text #1 +9 lines]"
        # Deleting the paste marker must not touch the image attachment.
        await pilot.press("backspace")
        await pilot.pause()
        assert pi.text == "[Image #1]"
        assert pi.pastes == []
        assert len(pi.attachments) == 1
