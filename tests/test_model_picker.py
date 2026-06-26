import anyio
import pytest
from textual.app import App
from textual.widgets import OptionList

from marim_harness.interfaces.tui.model_picker import ModelPickerModal
from marim_harness.workspace import ModelEntry

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


class _PushHost(App):
    """Pushes the modal (not push_screen_wait) so its on_mount/worker run, the way
    the real app opens it — used to exercise the async catalog-loading path."""

    def __init__(self, modal):
        super().__init__()
        self._modal = modal
        self.result = "unset"

    def on_mount(self) -> None:
        self.push_screen(self._modal, lambda r: setattr(self, "result", r))


@pytest.mark.anyio
async def test_async_fetch_does_not_block_and_populates_when_it_returns():
    gate = anyio.Event()

    async def fetch():
        await gate.wait()
        return _ENTRIES

    modal = ModelPickerModal(fetch=fetch)
    app = _PushHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        opts = modal.query_one("#model-options", OptionList)
        assert opts.option_count == 0  # modal is up though the fetch is pending
        gate.set()
        await pilot.pause()
        await pilot.pause()
        assert opts.option_count == len(_ENTRIES)  # populated once it returned


@pytest.mark.anyio
async def test_free_text_works_before_catalog_loads():
    gate = anyio.Event()  # never set: the fetch stays pending the whole test

    async def fetch():
        await gate.wait()
        return _ENTRIES

    modal = ModelPickerModal(fetch=fetch)
    app = _PushHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "my/model":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "my/model"


@pytest.mark.anyio
async def test_empty_remote_catalog_allows_free_text_after_load():
    async def fetch():
        return []  # remote fetch failed/empty

    modal = ModelPickerModal(fetch=fetch, is_local=False)
    app = _PushHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        for ch in "raw/id":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "raw/id"


@pytest.mark.anyio
async def test_picker_option_id_is_qualified_and_label_tags_provider():
    entries = [ModelEntry(id="anthropic/c", name="Claude", provider="openrouter")]
    modal = ModelPickerModal(entries=entries, allow_free_text=True)
    app = _PushHost(modal)
    async with app.run_test() as pilot:
        await pilot.pause()
        opts = modal.query_one("#model-options", OptionList)
        opt = opts.get_option_at_index(0)
        assert opt.id == "openrouter:anthropic/c"
        assert "· openrouter" in str(opt.prompt)
