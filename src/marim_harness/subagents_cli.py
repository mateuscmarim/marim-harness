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

import asyncio
import contextlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
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


class CliStreamTranslator:
    """Turns parsed Claude Code stream-json objects into the Pydantic AI message
    events the TUI already renders, so a CLI spawn streams nested under its card
    like a native sub-agent. Stateful across a run: numbers parts and remembers
    each tool_use's name so the matching tool_result can be labeled. ``translate``
    returns zero or more events per object; ``system`` and the terminal ``result``
    yield nothing (the runner reads result text/usage separately).

    stream-json without ``--include-partial-messages`` delivers each assistant
    message whole, so a text block becomes an empty part-start plus one full
    delta — the render path's delta branch appends it exactly as for live tokens.
    """

    def __init__(self) -> None:
        self._index = 0
        self._call_names: dict[str, str] = {}

    def translate(self, obj: dict) -> list:
        kind = obj.get("type")
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        return []

    def _assistant(self, obj: dict) -> list:
        events: list = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=TextPart(content="")))
                events.append(PartDeltaEvent(
                    index=idx,
                    delta=TextPartDelta(content_delta=block.get("text", "")),
                ))
            elif btype == "tool_use":
                call_id = block.get("id", "")
                name = block.get("name", "tool")
                self._call_names[call_id] = name
                events.append(FunctionToolCallEvent(part=ToolCallPart(
                    tool_name=name,
                    args=block.get("input", {}),
                    tool_call_id=call_id,
                )))
        return events

    def _user(self, obj: dict) -> list:
        events: list = []
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            events.append(FunctionToolResultEvent(part=ToolReturnPart(
                tool_name=self._call_names.get(call_id, "tool"),
                content=_flatten_tool_result(block.get("content")),
                tool_call_id=call_id,
                timestamp=datetime.now(tz=timezone.utc),
                outcome="failed" if block.get("is_error") else "success",
            )))
        return events


def _flatten_tool_result(content) -> str:
    """A tool_result's content is either a string or a list of content blocks;
    reduce it to plain text for the card."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


class ClaudeCliRunner:
    """Spawns the Claude Code CLI for one sub-agent task and forwards its activity.

    Reads the process's stream-json stdout line by line, translates each event for
    the UI (when a foreground ``stream_id`` and an ``on_event`` sink are present),
    and captures the terminal ``result`` event's text + usage. Raises CliRunError
    if the process ends without a result. The harness wraps this with hooks,
    output cap, and worktree handling — see SubagentRunner._execute_cli_spawn.
    """

    def __init__(self, on_event, on_notice, on_model=None) -> None:
        self._on_event = on_event      # Deps.on_subagent_event | None
        # Reserved for the spec's low-fidelity on_subagent_notice fallback; not
        # wired in v1 — full-fidelity event translation via _on_event is always used.
        self._on_notice = on_notice    # Deps.on_subagent_notice | None
        # Surfaces the model the CLI reports (system/init) to the spawn card, which
        # otherwise shows the harness's own model as a fallback. None when no UI.
        self._on_model = on_model      # Deps.on_subagent_model | None

    async def run(
        self, *, binary: str, prompt: str, system_prompt: str, cwd: str,
        allow_gated: bool, allowed_tools, model: str | None, stream_id: str,
    ) -> CliResult:
        argv = build_cli_argv(
            binary, prompt, system_prompt,
            cli_permission_mode(allow_gated),
            map_tools_to_cc(allowed_tools), model,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Drain stderr concurrently: if the child floods stderr past the OS pipe
        # buffer before finishing stdout, a sequential "read stdout then stderr"
        # would deadlock (child blocks writing stderr, parent waits on stdout EOF).
        stderr_task = asyncio.ensure_future(proc.stderr.read()) if proc.stderr is not None else None
        try:
            translator = CliStreamTranslator()
            output = ""
            usage = RunUsage()
            result_seen = False
            model_sent = False
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise on stdout — skip
                if not model_sent:
                    # The system/init event carries the session model at top level;
                    # assistant messages carry it under message.model. Surface the
                    # first one seen so the card shows the CLI's real model. Guard
                    # message against a non-dict (malformed/future stream shape).
                    msg = obj.get("message")
                    found = obj.get("model") or (
                        msg.get("model") if isinstance(msg, dict) else None
                    )
                    if found:
                        model_sent = True
                        if self._on_model is not None and stream_id:
                            await self._on_model(stream_id, str(found))
                if obj.get("type") == "result":
                    output = obj.get("result", "") or ""
                    usage = synth_usage(obj.get("usage"), obj.get("num_turns", 0) or 0)
                    result_seen = True
                    continue
                for event in translator.translate(obj):
                    if self._on_event is not None and stream_id:
                        await self._on_event(stream_id, event, None)
            stderr_bytes = await stderr_task if stderr_task is not None else b""
            stderr_task = None  # consumed — don't cancel it in finally
            code = await proc.wait()
            if not result_seen:
                detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {code}"
                raise CliRunError(f"claude produced no result ({detail})")
            return CliResult(output=output, usage=usage)
        finally:
            # On an exceptional/cancelled exit, reap the child so an auto-mode CLI
            # can't keep editing files after the spawn was abandoned, and never leave
            # stderr_task un-retrieved (which would log an asyncio "Future destroyed"
            # warning and suppress the real exception on Python 3.11+).
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(BaseException):
                    await stderr_task
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await proc.wait()
