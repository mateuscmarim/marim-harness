from marim_harness.runtime.context import plan_mode_preamble, render_shell_results_block


def test_plan_mode_preamble_mentions_present_plan_and_read_only():
    text = plan_mode_preamble()
    assert "present_plan" in text
    assert "PLAN MODE" in text


def test_render_shell_results_block_empty_is_falsy():
    assert render_shell_results_block([]) == ""


def test_render_shell_results_block_formats_commands_and_output():
    block = render_shell_results_block(
        [("git status", "exit 0\nclean"), ("ls", "exit 0\nfoo.py")]
    )
    assert block.startswith("<user-shell-commands>")
    assert block.endswith("</user-shell-commands>")
    assert "$ git status" in block
    assert "exit 0\nclean" in block
    assert "$ ls" in block
    assert "elided" not in block  # no drop marker when nothing was dropped


def test_render_shell_results_block_notes_dropped_entries():
    block = render_shell_results_block([("ls", "exit 0\nfoo.py")], dropped=2)
    assert "2 earlier command(s) elided" in block
