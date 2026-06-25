"""Run the Claude Code CLI (`claude -p`) as a sub-agent backend.

An authored agent with `backend: claude-cli` is spawned as an external `claude`
process in headless stream-json mode instead of the in-process Pydantic AI loop.
This module is backend-only: the pure translation helpers (binary resolve, argv
build, harness→Claude-Code tool-name mapping, permission-mode selection, usage
synthesis), the stream-event translator, and the thin `ClaudeCliRunner` that
spawns the process and forwards its activity to the UI. The harness wrapping
(worktree, hooks bracketing, output cap, background persist) stays in
`subagents.py`, so this module is unit-tested without the rest of the harness.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

from pydantic_ai.usage import RunUsage

logger = logging.getLogger(__name__)

CLI_BINARY_ENV = "MARIM_CLAUDE_CLI_BIN"
CLI_MODEL_ENV = "MARIM_CLAUDE_CLI_MODEL"

# Harness tool name → Claude Code tool name. Names with no Claude Code equivalent
# (tree, the LSP navigation tools) are absent on purpose: the CLI has its own
# navigation, so we don't fabricate a mapping. The result feeds --allowedTools.
_CC_TOOL_MAP = {
    "read_file": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "web_search": "WebSearch",
    "fetch_url": "WebFetch",
    "write_file": "Write",
    "edit_file": "Edit",
    "bash": "Bash",
}


class CliUnavailable(Exception):
    """No `claude` binary could be found to back a claude-cli spawn."""


class CliRunError(Exception):
    """The CLI ran but produced no terminal result event (crash / bad output)."""


@dataclass
class CliResult:
    """A finished CLI spawn, shaped like the bits of a Pydantic AI run result the
    spawn lifecycle consumes: the final report text and the run's usage."""

    output: str
    usage: RunUsage


def resolve_cli_binary() -> str | None:
    """The Claude Code executable to spawn: ``$MARIM_CLAUDE_CLI_BIN`` if set, else
    ``claude`` on PATH. Returns an absolute path, or None when nothing is found so
    the caller reports a clean error instead of crashing."""
    name = os.environ.get(CLI_BINARY_ENV) or "claude"
    return shutil.which(name)


def cli_permission_mode(allow_gated: bool) -> str:
    """The ``--permission-mode`` for a spawn: ``acceptEdits`` in auto mode (gated
    tools allowed), else ``plan`` (read-only — the headless CLI can't prompt, so
    anything not pre-authorized is simply unavailable)."""
    return "acceptEdits" if allow_gated else "plan"


def map_tools_to_cc(tool_names) -> list[str]:
    """Translate granted harness tool names to Claude Code ``--allowedTools``
    names, dropping any without a Claude Code equivalent. Sorted for a stable
    argv (and stable tests)."""
    return sorted({_CC_TOOL_MAP[n] for n in tool_names if n in _CC_TOOL_MAP})


def build_cli_argv(
    binary: str,
    prompt: str,
    system_prompt: str,
    permission_mode: str,
    allowed_tools: list[str],
    model: str | None,
) -> list[str]:
    """The argv for one headless spawn. ``stream-json`` requires ``--verbose``.
    The task is a single positional arg (we exec, not shell — no quoting hazard);
    the agent's role prompt is appended to the CLI's own system prompt. ``--model``
    is omitted when None so the CLI uses its configured default; ``--allowedTools``
    is omitted when empty (which, in plan mode, simply leaves the CLI read-only)."""
    argv = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", system_prompt,
        "--permission-mode", permission_mode,
    ]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if model:
        argv += ["--model", model]
    return argv


def synth_usage(cli_usage: dict | None, num_turns: int) -> RunUsage:
    """Build a RunUsage from the CLI ``result`` event's ``usage`` block so the
    turn's token line reflects the spawn. Only tokens are folded — the dollar cost
    is the CLI account's, not the harness provider's. Missing keys default to 0."""
    u = cli_usage or {}
    return RunUsage(
        input_tokens=int(u.get("input_tokens", 0) or 0),
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        requests=int(num_turns or 0),
    )
