"""The advisor renders standalone (outside the collapsed tool-run group), the
same treatment ask_user gets — the advice is conversation content, not
mechanical work to fold behind a '≡ N tools' group."""

from marim_harness.interfaces.tui.stream_render import _STANDALONE_TOOLS


def test_advisor_and_ask_user_render_standalone():
    assert "advisor" in _STANDALONE_TOOLS
    assert "ask_user" in _STANDALONE_TOOLS
    # spawn_agent has its own SubAgentWidget claim path, not this one.
    assert "spawn_agent" not in _STANDALONE_TOOLS
