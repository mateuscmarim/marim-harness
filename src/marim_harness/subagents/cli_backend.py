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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

if TYPE_CHECKING:
    from .cli_demux import RoutedEvent

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

# Claude Code tool name → harness tool name (the inverse of _CC_TOOL_MAP). The TUI
# keys all its rich rendering (the edit diff, write/read highlighting, the tool
# summary labels) on harness names and arg shapes, so a CLI sub-agent's events are
# normalized back to those before they reach the renderer — otherwise an `Edit`
# falls through to a raw-args dump instead of the inline diff a native edit shows.
_HARNESS_TOOL_MAP = {cc: harness for harness, cc in _CC_TOOL_MAP.items()}


def normalize_cc_tool(name: str, args: dict) -> tuple[str, dict]:
    """Map a Claude Code tool_use (name + ``input``) to the harness tool name and
    arg shape the TUI widgets expect, so a CLI spawn renders identically to a
    native call. Unmapped tools (TodoWrite, Task, …) pass through unchanged for
    generic rendering.

    The harness ``fs.Edit`` model already shares Claude Code's field names
    (``old_string``/``new_string``/``replace_all``), so an Edit maps to a single
    such edit wrapped in the ``edits`` list ``edit_file`` renders from. Read/Write
    just rename ``file_path`` → ``path``; the rest share their arg keys
    (``pattern``/``command``/``query``)."""
    harness = _HARNESS_TOOL_MAP.get(name)
    if harness is None:
        return name, args
    if name == "Edit":
        return harness, {
            "path": args.get("file_path", ""),
            "edits": [{
                "old_string": args.get("old_string", ""),
                "new_string": args.get("new_string", ""),
                "replace_all": bool(args.get("replace_all", False)),
            }],
        }
    if name in ("Read", "Write") and "file_path" in args:
        out = {k: v for k, v in args.items() if k != "file_path"}
        out["path"] = args["file_path"]
        return harness, out
    return harness, args


class CliUnavailable(Exception):
    """No `claude` binary could be found to back a claude-cli spawn."""


class CliRunError(Exception):
    """The CLI ran but produced no terminal result event (crash / bad output)."""


@dataclass
class CliResult:
    """A finished CLI spawn, shaped like the bits of a Pydantic AI run result the
    spawn lifecycle consumes: the final report text, the run's usage, and the
    transcripts — the parent's, plus one per Claude-side child sub-agent (keyed
    by the child's stream id) for sidecar persistence."""

    output: str
    usage: RunUsage
    transcript: list = field(default_factory=list)
    child_transcripts: dict = field(default_factory=dict)


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
    *,
    resume_session_id: str | None = None,
    append_system: bool = True,
    safe_mode: bool = False,
) -> list[str]:
    """The argv for one headless spawn. ``stream-json`` requires ``--verbose``.
    The task is a single positional arg (we exec, not shell — no quoting hazard);
    the agent's role prompt is appended to the CLI's own system prompt. ``--model``
    is omitted when None so the CLI uses its configured default; ``--allowedTools``
    is omitted when empty (which, in plan mode, simply leaves the CLI read-only).

    The main-loop ``ClaudeCliModel`` uses ``resume_session_id`` to continue an
    existing Claude session (sending only the new user message), and sets
    ``append_system=False`` on those resumed turns so the system prompt — already
    set when the session was created — is not appended again. It also sets
    ``safe_mode`` so ``--safe-mode`` disables the user's plugins/hooks (e.g.
    agentmemory's SessionStart context injection, which otherwise bleeds
    cross-session observations into the turn and derails Claude); auth, model, and
    built-in tools still work normally."""
    argv = [
        binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", permission_mode,
    ]
    if safe_mode:
        argv.append("--safe-mode")
    if append_system:
        argv += ["--append-system-prompt", system_prompt]
    if resume_session_id:
        argv += ["--resume", resume_session_id]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    if model:
        argv += ["--model", model]
    return argv


def synth_usage(
    cli_usage: dict | None,
    num_turns: int,
    total_cost_usd: float | None = None,
) -> RunUsage:
    """Build a RunUsage from the CLI ``result`` event's ``usage`` block.
    When ``total_cost_usd`` is provided (the CLI's billed amount), it is stored
    in ``details[COST_DETAIL_KEY]`` as integer micro-USD so ``resolve_cost``
    surfaces it as the exact cost — no model-id lookup needed. Missing token
    keys default to 0.

    The CLI reports Anthropic's *raw* usage, where ``input_tokens`` is the
    uncached prompt tokens only — the cache read/write buckets are reported
    separately and are NOT included in it. But the rest of the harness (and
    pydantic-ai/genai-prices for the native path) treats ``RunUsage.input_tokens``
    as *inclusive* of cache: ``split_tokens`` recovers the uncached bucket as
    ``input_tokens - cache_read - cache_write``, and ``total_tokens`` is
    ``input_tokens + output_tokens``. So we fold the cache buckets into
    ``input_tokens`` here, exactly as genai-prices does for a native Anthropic
    response. Without this the uncached split underflowed to 0 (the reported ``↑``
    was always zero) and the token total omitted all cached tokens."""
    from ..usage import COST_DETAIL_KEY

    u = cli_usage or {}
    details: dict = {}
    if total_cost_usd is not None:
        details[COST_DETAIL_KEY] = int(total_cost_usd * 1_000_000)
    uncached_in = int(u.get("input_tokens", 0) or 0)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
    return RunUsage(
        input_tokens=uncached_in + cache_read + cache_write,  # inclusive of cache
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        requests=int(num_turns or 0),
        details=details,
    )


def sum_result_usages(results: list[dict]) -> tuple[dict, int, float | None]:
    """Fold one CLI run's ``result`` events into ``(usage, num_turns, cost)``
    ready for ``synth_usage`` / ``request_usage_from_cli``.

    One ``claude -p`` process can emit SEVERAL result events: an async
    sub-agent's completion notification re-invokes the main agent, which ends
    in another result. Token buckets are per-segment, so they are summed;
    ``total_cost_usd`` is cumulative across the whole process, so the LAST
    value is the run's cost (both verified against a live 2.1.198 stream).
    Nested non-numeric usage values (``cache_creation``, ``server_tool_use``)
    are skipped."""
    summed: dict = {}
    turns = 0
    cost: float | None = None
    for r in results:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                summed[k] = summed.get(k, 0) + v
        turns += int(r.get("num_turns", 0) or 0)
        if r.get("total_cost_usd") is not None:
            cost = float(r["total_cost_usd"])
    return summed, turns, cost


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
    Thinking blocks render as collapsed thoughts via PartStartEvent(ThinkingPart) +
    PartDeltaEvent(ThinkingPartDelta). The ``record_call`` and ``record_return``
    methods exist for the demux to synthesize tool call/return pairs (e.g. for
    Claude's own sub-agents) into the transcript so a persisted sidecar never
    carries an unanswered call.
    """

    def __init__(self) -> None:
        self._index = 0
        self._call_names: dict[str, str] = {}
        self._messages: list = []

    def translate(self, obj: dict) -> list:
        kind = obj.get("type")
        if kind == "assistant":
            return self._assistant(obj)
        if kind == "user":
            return self._user(obj)
        return []

    def _assistant(self, obj: dict) -> list:
        events: list = []
        resp_parts = []
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
                resp_parts.append(TextPart(content=block.get("text", "")))
            elif btype == "thinking":
                idx = self._index
                self._index += 1
                events.append(PartStartEvent(index=idx, part=ThinkingPart(content="")))
                events.append(PartDeltaEvent(
                    index=idx,
                    delta=ThinkingPartDelta(content_delta=block.get("thinking", "")),
                ))
                resp_parts.append(ThinkingPart(content=block.get("thinking", "")))
            elif btype == "tool_use":
                call_id = block.get("id", "")
                name, args = normalize_cc_tool(
                    block.get("name", "tool"), block.get("input", {}) or {},
                )
                self._call_names[call_id] = name  # the matching result reuses it
                events.append(FunctionToolCallEvent(part=ToolCallPart(
                    tool_name=name,
                    args=args,
                    tool_call_id=call_id,
                )))
                resp_parts.append(ToolCallPart(
                    tool_name=name, args=args, tool_call_id=call_id))
        if resp_parts:
            self._messages.append(ModelResponse(parts=resp_parts))
        return events

    def _user(self, obj: dict) -> list:
        events: list = []
        req_parts = []
        for block in obj.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            part = ToolReturnPart(
                tool_name=self._call_names.get(call_id, "tool"),
                content=_flatten_tool_result(block.get("content")),
                tool_call_id=call_id,
                timestamp=datetime.now(tz=timezone.utc),
                outcome="failed" if block.get("is_error") else "success",
            )
            events.append(FunctionToolResultEvent(part=part))
            req_parts.append(part)
        if req_parts:
            self._messages.append(ModelRequest(parts=req_parts))
        return events

    def transcript(self) -> list:
        """The run so far as pydantic-ai messages (for transcript persistence)."""
        return list(self._messages)

    def record_call(self, part: ToolCallPart) -> None:
        """Append a synthesized tool call (e.g. the demux's spawn_agent for a
        Claude-side sub-agent) to the transcript, and remember its name so a
        later synthesized return — or a raw tool_result hitting translate() —
        labels itself correctly."""
        self._call_names[part.tool_call_id] = part.tool_name
        self._messages.append(ModelResponse(parts=[part]))

    def record_return(self, part: ToolReturnPart) -> None:
        """Append a synthesized tool return, closing a record_call so a
        persisted sidecar never carries an unanswered call."""
        self._messages.append(ModelRequest(parts=[part]))


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


_READ_CHUNK = 65536


async def _iter_ndjson_lines(stream, chunk_size: int = _READ_CHUNK):
    """Yield decoded newline-delimited lines from ``stream`` with no per-line
    length cap.

    ``async for line in stream`` (and ``StreamReader.readline``) caps a line at
    asyncio's 64 KiB buffer limit and raises ``ValueError: Separator is found,
    but chunk is longer than limit`` on anything longer. The Claude CLI emits one
    JSON object per line, and a line carrying a ``tool_result`` with file
    contents (a single Read of a large source file) routinely exceeds 64 KiB — so
    that cap crashed otherwise-fine spawns. Reading raw chunks and splitting on
    ``\\n`` ourselves removes the cap (bounded only by available memory). Decoding
    one complete line at a time is safe: a ``\\n`` byte never falls inside a UTF-8
    multibyte sequence, so no character is split across the boundary."""
    buffer = b""
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        buffer += chunk
        while True:
            nl = buffer.find(b"\n")
            if nl < 0:
                break
            line, buffer = buffer[:nl], buffer[nl + 1:]
            yield line.decode("utf-8", "replace")
    if buffer:  # a final line with no trailing newline
        yield buffer.decode("utf-8", "replace")


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
            from .cli_demux import CliSubagentDemux  # lazy: cli_demux imports us

            translator = CliStreamTranslator()
            demux = CliSubagentDemux()
            output = ""
            results: list[dict] = []
            model_sent = False
            assert proc.stdout is not None
            async for raw in _iter_ndjson_lines(proc.stdout):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise on stdout — skip
                # Claude-side sub-agent traffic (Agent/Task spawns, their child
                # streams, task lifecycle events) is demuxed into per-card
                # streams; whatever remains is this spawn's own main stream.
                routed, remainder = demux.route(obj)
                for r in routed:
                    await self._deliver(r, translator, stream_id)
                if remainder is None:
                    continue
                obj = remainder
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
                    # One -p process can emit several results (an async
                    # sub-agent's completion notification re-invokes the main
                    # agent). The LAST result's text is the final report;
                    # usage folds across all of them (sum_result_usages).
                    results.append(obj)
                    output = obj.get("result", "") or ""
                    continue
                for event in translator.translate(obj):
                    if self._on_event is not None and stream_id:
                        await self._on_event(stream_id, event, None)
            stderr_bytes = await stderr_task if stderr_task is not None else b""
            stderr_task = None  # consumed — don't cancel it in finally
            code = await proc.wait()
            if not results:
                detail = stderr_bytes.decode("utf-8", "replace").strip() or f"exit code {code}"
                raise CliRunError(f"claude produced no result ({detail})")
            return CliResult(
                output=output,
                usage=synth_usage(*sum_result_usages(results)),
                transcript=translator.transcript(),
                child_transcripts=demux.child_transcripts(),
            )
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

    async def _deliver(
        self, routed: RoutedEvent, translator: CliStreamTranslator, stream_id: str
    ) -> None:
        """Forward one demux-routed event. Main-routed events (the synthesized
        spawn_agent call/return for a Claude-side spawn) go to this spawn's own
        stream and are recorded into the parent transcript, so the persisted
        sidecar replays the nested card. Child-routed events go to the child's
        stream with its live usage and (once) its reported model."""
        if routed.stream_id is None:
            part = getattr(routed.event, "part", None)
            if isinstance(part, ToolCallPart):
                translator.record_call(part)
            elif isinstance(part, ToolReturnPart):
                translator.record_return(part)
            if self._on_event is not None and stream_id:
                await self._on_event(stream_id, routed.event, None)
            return
        if routed.model and self._on_model is not None:
            await self._on_model(routed.stream_id, routed.model)
        if self._on_event is not None:
            await self._on_event(routed.stream_id, routed.event, routed.usage)
