from marim_harness.session import SessionInfo
from marim_harness.tui.commands import COMMANDS, COMMANDS_BY_NAME, resolve_ref


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
    for name in ("help", "clear", "sessions", "new", "switch", "mode", "model", "exit"):
        assert name in COMMANDS_BY_NAME
