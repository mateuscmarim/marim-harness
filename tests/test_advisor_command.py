"""/advisor dispatch: off, direct slug, and blank-opens-picker."""

from types import SimpleNamespace

import pytest

from marim_harness.interfaces.tui.commands import COMMANDS_BY_NAME, dispatch


class _App:
    def __init__(self):
        self.posted: list[str] = []
        self.picker_opened = False
        self.advisor_calls: list = []
        self.harness = SimpleNamespace(
            set_advisor_model=lambda mid: self.advisor_calls.append(mid),
        )
        self.pickers = SimpleNamespace(open_advisor=self._open_advisor)

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    async def _open_advisor(self) -> None:
        self.picker_opened = True


def test_advisor_command_registered():
    assert "advisor" in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_advisor_off_disables_and_confirms():
    app = _App()
    await dispatch(app, "/advisor off")
    assert app.advisor_calls == [None]
    assert any("off" in p for p in app.posted)


@pytest.mark.anyio
async def test_advisor_with_slug_sets_it():
    app = _App()
    await dispatch(app, "/advisor openrouter:anthropic/claude-opus-4.8")
    assert app.advisor_calls == ["openrouter:anthropic/claude-opus-4.8"]
    assert any("openrouter:anthropic/claude-opus-4.8" in p for p in app.posted)


@pytest.mark.anyio
async def test_advisor_blank_opens_picker():
    app = _App()
    await dispatch(app, "/advisor")
    assert app.picker_opened
    assert app.advisor_calls == []
