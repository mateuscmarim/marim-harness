"""claude/env.py: pure lookups for the Claude Code binary and timeouts."""

from __future__ import annotations

import sys

from marim_harness.claude import env


def test_resolve_binary_prefers_env_override(monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", sys.executable)
    assert env.resolve_cli_binary() == sys.executable


def test_resolve_binary_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", str(tmp_path / "nope"))
    assert env.resolve_cli_binary() is None


def test_cli_timeout_defaults_and_rejects_garbage(monkeypatch):
    monkeypatch.delenv("MARIM_CLAUDE_CLI_TIMEOUT", raising=False)
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "abc")
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0")
    assert env.cli_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "12.5")
    assert env.cli_timeout() == 12.5


def test_idle_timeout_zero_means_never(monkeypatch):
    monkeypatch.delenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", raising=False)
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "0")
    assert env.cli_idle_timeout() == 0.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "-3")
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "garbage")
    assert env.cli_idle_timeout() == 600.0
    monkeypatch.setenv("MARIM_CLAUDE_CLI_IDLE_TIMEOUT", "30")
    assert env.cli_idle_timeout() == 30.0


def test_legacy_names_still_importable_from_cli_backend():
    from marim_harness.subagents import cli_backend

    assert cli_backend.CLI_BINARY_ENV == "MARIM_CLAUDE_CLI_BIN"
    assert cli_backend.resolve_cli_binary is env.resolve_cli_binary
    assert cli_backend._cli_timeout is env.cli_timeout
    assert cli_backend._DEFAULT_CLI_TIMEOUT == 600.0
