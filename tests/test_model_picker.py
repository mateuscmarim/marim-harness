import pytest
from textual.app import App

from marim_harness.workspace import ModelEntry
from marim_harness.tui.model_picker import ModelPickerModal

_ENTRIES = [
    ModelEntry(id="anthropic/claude-sonnet-4-6", name="Claude Sonnet 4.6"),
    ModelEntry(id="openai/gpt-5.2", name="GPT-5.2"),
    ModelEntry(id="xiaomi/mimo-v2.5", name="MiMo v2.5"),
]


class _Host(App):
    def __init__(self, entries, allow_free_text=False):
        super().__init__()
        self.entries = entries
        self.allow_free_text = allow_free_text
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._pick())

    async def _pick(self) -> None:
        self.result = await self.push_screen_wait(
            ModelPickerModal(self.entries, allow_free_text=self.allow_free_text)
        )


@pytest.mark.anyio
async def test_filter_then_enter_picks_highlighted():
    app = _Host(_ENTRIES)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "gpt":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "openai/gpt-5.2"


@pytest.mark.anyio
async def test_escape_cancels_with_none():
    app = _Host(_ENTRIES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_free_text_entry_when_no_catalog():
    app = _Host([], allow_free_text=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "my-local-model":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "my-local-model"


@pytest.mark.anyio
async def test_empty_enter_does_not_dismiss_without_free_text():
    app = _Host([], allow_free_text=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # nothing highlighted, free text off
        await pilot.pause()
        assert app.result == "unset"  # still open
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None
