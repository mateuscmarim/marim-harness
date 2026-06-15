import pytest
from textual.app import App

from marim_harness.tui.approval import ApprovalModal


class _Harness(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await self.push_screen_wait(
            ApprovalModal("edit_file", {"path": "a.txt"})
        )


@pytest.mark.anyio
async def test_approve_returns_true():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")  # approve binding
        await pilot.pause()
    assert app.result is True


@pytest.mark.anyio
async def test_deny_returns_false():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")  # deny binding
        await pilot.pause()
    assert app.result is False
