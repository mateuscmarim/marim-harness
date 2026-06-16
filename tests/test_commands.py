import pytest

from marim_harness.session import SessionInfo
from marim_harness.tui.commands import (
    COMMANDS,
    COMMANDS_BY_NAME,
    dispatch,
    resolve_ref,
)


class _FakeApp:
    """Minimal stand-in for HarnessApp: records posts and spawned turns."""

    def __init__(self):
        self.posted: list[str] = []
        self.turn_prompts: list[str] = []
        self._turn_worker = None
        self._current_assistant = "sentinel"

    async def post_system(self, msg: str) -> None:
        self.posted.append(msg)

    def _run_turn(self, text: str):
        self.turn_prompts.append(text)
        return ("coro", text)  # stand-in; not awaited by the fake

    def run_worker(self, coro, exclusive=False):
        return ("worker", coro)


def _infos() -> list:
    return [
        SessionInfo(id="alpha", name="Alpha", updated="2026-06-01", message_count=2, tokens=10),
        SessionInfo(id="beta", name="Beta Work", updated="2026-05-01", message_count=4, tokens=20),
    ]


def test_resolve_ref_by_index():
    infos = _infos()
    assert resolve_ref(infos, "1") is infos[0]
    assert resolve_ref(infos, "2") is infos[1]


def test_resolve_ref_by_id_and_name():
    infos = _infos()
    assert resolve_ref(infos, "beta") is infos[1]
    assert resolve_ref(infos, "beta work") is infos[1]  # name, case-insensitive


def test_resolve_ref_misses():
    infos = _infos()
    assert resolve_ref(infos, "9") is None
    assert resolve_ref(infos, "0") is None
    assert resolve_ref(infos, "nope") is None
    assert resolve_ref(infos, "") is None


def test_every_command_has_summary_and_handler():
    for cmd in COMMANDS:
        assert cmd.summary
        assert callable(cmd.handler)


def test_aliases_resolve_to_their_command():
    assert COMMANDS_BY_NAME["quit"] is COMMANDS_BY_NAME["exit"]
    assert COMMANDS_BY_NAME["ls"] is COMMANDS_BY_NAME["sessions"]
    assert COMMANDS_BY_NAME["?"] is COMMANDS_BY_NAME["help"]


def test_new_is_its_own_command_not_a_clear_alias():
    assert COMMANDS_BY_NAME["new"] is not COMMANDS_BY_NAME["clear"]
    assert COMMANDS_BY_NAME["new"].name == "new"


def test_core_commands_present():
    names = ("help", "clear", "sessions", "new", "switch", "name", "mode", "model",
             "remember", "exit")
    for name in names:
        assert name in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_remember_empty_arg_shows_usage():
    app = _FakeApp()
    await dispatch(app, "/remember")
    assert app.turn_prompts == []  # no turn spawned
    assert app.posted and "usage" in app.posted[0].lower()


@pytest.mark.anyio
async def test_remember_spawns_turn_with_fact_and_tool_instruction():
    app = _FakeApp()
    await dispatch(app, "/remember the build uses uv")
    assert len(app.turn_prompts) == 1
    prompt = app.turn_prompts[0]
    assert "the build uses uv" in prompt
    assert "remember tool" in prompt
    assert app._turn_worker is not None
