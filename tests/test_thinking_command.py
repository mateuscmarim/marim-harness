"""/think dispatch: a direct level, off, an unknown level, and blank-opens-picker."""

from types import SimpleNamespace

import pytest

from marim_harness.interfaces.tui.commands import COMMANDS_BY_NAME, dispatch


class _App:
    def __init__(self):
        self.posted: list[str] = []
        self.picker_opened = False
        self.calls: list = []
        self.harness = SimpleNamespace(
            set_thinking_level=lambda level: self.calls.append(level),
            thinking_level_id=None,
        )

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    async def open_thinking_picker(self) -> None:
        self.picker_opened = True


def test_think_command_registered():
    assert "think" in COMMANDS_BY_NAME
    assert "effort" in COMMANDS_BY_NAME  # alias


@pytest.mark.anyio
async def test_think_with_level_sets_it():
    app = _App()
    await dispatch(app, "/think high")
    assert app.calls == ["high"]
    assert any("high" in p for p in app.posted)


@pytest.mark.anyio
async def test_think_off_disables():
    app = _App()
    await dispatch(app, "/think off")
    assert app.calls == ["off"]


@pytest.mark.anyio
async def test_think_unknown_level_is_rejected_without_setting():
    app = _App()
    await dispatch(app, "/think ultra")
    assert app.calls == []
    assert any("ultra" in p or "unknown" in p.lower() for p in app.posted)


@pytest.mark.anyio
async def test_think_blank_opens_picker():
    app = _App()
    await dispatch(app, "/think")
    assert app.picker_opened
    assert app.calls == []
