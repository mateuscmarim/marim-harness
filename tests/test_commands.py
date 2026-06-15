from marim_harness.tui.commands import COMMANDS, COMMANDS_BY_NAME


def test_every_command_has_summary_and_handler():
    for cmd in COMMANDS:
        assert cmd.summary
        assert callable(cmd.handler)


def test_aliases_resolve_to_their_command():
    assert COMMANDS_BY_NAME["quit"] is COMMANDS_BY_NAME["exit"]
    assert COMMANDS_BY_NAME["new"] is COMMANDS_BY_NAME["clear"]
    assert COMMANDS_BY_NAME["?"] is COMMANDS_BY_NAME["help"]


def test_core_commands_present():
    for name in ("help", "clear", "mode", "model", "exit"):
        assert name in COMMANDS_BY_NAME
