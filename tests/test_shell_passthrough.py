import re
from pathlib import Path

import pytest
from textual.app import App

from marim_harness.interfaces.tui.shell_passthrough import (
    PASSTHROUGH_TIMEOUT,
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    rewrite_sudo,
    run_passthrough,
)


def test_parse_bang_with_and_without_space():
    assert parse_bang("! git status") == "git status"
    assert parse_bang("!git status") == "git status"


def test_parse_bang_bare_bang_is_empty_string():
    assert parse_bang("!") == ""


def test_parse_bang_non_bang_text_is_none():
    assert parse_bang("git status") is None
    assert parse_bang("/help") is None
    assert parse_bang("") is None


def test_needs_sudo_password_matches_leading_token_only():
    assert needs_sudo_password("sudo systemctl restart nginx")
    assert not needs_sudo_password("echo sudo")
    assert not needs_sudo_password("sudoedit /etc/hosts")
    # Mid-pipeline sudo is out of scope (spec): it fails with sudo's own error.
    assert not needs_sudo_password("foo | sudo tee /etc/x")


def test_rewrite_sudo_inserts_stdin_flags():
    assert rewrite_sudo("sudo apt update") == "sudo -S -p '' -k apt update"


def test_format_transcript_block_echoes_command_and_fences_output():
    block = format_transcript_block("echo hi", "exit 0\nhi")
    assert "! echo hi" in block
    assert "```" in block
    assert "exit 0\nhi" in block


def test_format_transcript_block_survives_backtick_fences_in_output():
    output = "```python\nprint('hi')\n```"
    block = format_transcript_block("cat README.md", output)
    lines = block.split("\n")
    opening_fence_line = lines[2]  # "{fence}text"
    closing_fence = lines[-1]
    assert opening_fence_line.endswith("text")
    opening_fence = opening_fence_line[: -len("text")]
    longest_run_in_output = max(len(m) for m in re.findall(r"`+", output))
    # Opening and closing fences match, and both are strictly longer than any
    # backtick run embedded in the output — so the embedded ``` can't close
    # the block early.
    assert opening_fence == closing_fence
    assert len(opening_fence) > longest_run_in_output


@pytest.mark.anyio
async def test_run_passthrough_runs_plain_command(tmp_path: Path):
    out = await run_passthrough(tmp_path, "echo pass-through")
    assert out.startswith("exit 0")
    assert "pass-through" in out


@pytest.mark.anyio
async def test_run_passthrough_password_feeds_stdin_never_output(
    tmp_path: Path, monkeypatch
):
    """Real sudo can't run in tests: capture the run_bash call instead and
    assert the rewrite + stdin plumbing, and that the password can't leak into
    the returned text."""
    captured = {}

    async def fake_run_bash(root, command, timeout=30, stdin_data=None):
        captured["command"] = command
        captured["timeout"] = timeout
        captured["stdin"] = stdin_data
        return "exit 0\nroot"

    monkeypatch.setattr(
        "marim_harness.interfaces.tui.shell_passthrough.run_bash", fake_run_bash
    )
    out = await run_passthrough(tmp_path, "sudo whoami", password="hunter2")
    assert captured["command"] == "sudo -S -p '' -k whoami"
    assert captured["stdin"] == b"hunter2\n"
    assert captured["timeout"] == PASSTHROUGH_TIMEOUT
    assert "hunter2" not in out


class _ModalHost(App):
    """Bare host app so the modal can be driven with Pilot."""


@pytest.mark.anyio
async def test_sudo_password_modal_submits_password():
    app = _ModalHost()
    async with app.run_test() as pilot:
        results: list = []
        app.push_screen(SudoPasswordModal("sudo whoami"), results.append)
        await pilot.pause()
        await pilot.press(*"hunter2", "enter")  # Input is focused on mount
        await pilot.pause()
        assert results == ["hunter2"]


@pytest.mark.anyio
async def test_sudo_password_modal_escape_cancels():
    app = _ModalHost()
    async with app.run_test() as pilot:
        results: list = []
        app.push_screen(SudoPasswordModal("sudo whoami"), results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert results == [None]
