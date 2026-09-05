"""Environment probes for the Claude Code CLI: binary and timeouts.

Pure lookups (``shutil.which`` and env reads) so the settings screen and
provider detection can call these freely; nothing here spawns a process.
"""

from __future__ import annotations

import os
import shutil

CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"
# Default model for `backend: claude-cli` sub-agents (the spec's `model:` wins;
# the spawn call's model= wins over both). Unset ⇒ the CLI's own default.
CLI_MODEL_ENV = "MARIM_CLAUDE_CLI_MODEL"
# Silence bound for one open turn (seconds between stream objects, excluding
# time an approval prompt is open in the panel).
CLI_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_TIMEOUT"
# How long a main-loop `claude` may sit with no open turn before marim closes
# it (the next turn resumes the session by id). `0` = never reap.
CLI_IDLE_TIMEOUT_ENV = "MARIM_CLAUDE_CLI_IDLE_TIMEOUT"
DEFAULT_CLI_TIMEOUT = 600.0
DEFAULT_IDLE_TIMEOUT = 600.0
# `--permission-prompt-tool stdio` + `--input-format stream-json` landed in the
# 2.1 line; older binaries reject the argv and exit before `system/init`.
MIN_CLAUDE_VERSION = "2.1"
INSTALL_HINT = (
    "Install Claude Code (npm i -g @anthropic-ai/claude-code) and sign in; "
    f"marim needs claude >= {MIN_CLAUDE_VERSION}. Set MARIM_CLAUDE_CLI_BIN to "
    "point at a specific binary."
)


class CliUnavailable(Exception):
    """The Claude Code CLI is missing (or too old to speak stream-json control)."""


def resolve_cli_binary() -> str | None:
    """The Claude Code executable to spawn: ``$MARIM_CLAUDE_CLI_BIN`` if set,
    else ``claude`` on PATH. Absolute path, or None when nothing is found so
    the caller reports a clean error instead of crashing."""
    return shutil.which(os.environ.get(CLI_BINARY_ENV) or "claude")


def _positive_float(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def cli_timeout() -> float:
    """Silence timeout (seconds) for one open turn; ``MARIM_CLAUDE_CLI_TIMEOUT``
    or 600. Garbage and non-positive values fall back to the default."""
    return _positive_float(os.environ.get(CLI_TIMEOUT_ENV, ""), DEFAULT_CLI_TIMEOUT)


def cli_idle_timeout() -> float:
    """Idle-reaper delay (seconds); ``MARIM_CLAUDE_CLI_IDLE_TIMEOUT`` or 600.
    ``0`` disables reaping (returns 0.0); negatives and garbage fall back."""
    raw = os.environ.get(CLI_IDLE_TIMEOUT_ENV, "")
    if raw.strip() == "0":
        return 0.0
    return _positive_float(raw, DEFAULT_IDLE_TIMEOUT)
