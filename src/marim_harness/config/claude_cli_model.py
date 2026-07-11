"""Run the Claude Code CLI (`claude -p`) as a main-loop model provider.

A Claude subscription is reachable only through the `claude` CLI, which runs its
own agentic loop — there is no raw per-step model endpoint behind it. So this
provider makes marim a *launcher*: ``ClaudeCliModel`` spawns ``claude -p`` in
stream-json mode, lets Claude run its own tools internally, and returns a single
**text-only** ``ModelResponse``. Emitting ``ToolCallPart``s here would make
pydantic_ai's agent graph try to execute Claude's tool calls a second time, so
Claude's internal tool activity is folded into the streamed text instead (see
``format_activity_line`` / ``consume_cli_stream``). Claude's own Agent/Task
sub-agent spawns are the exception: they are split off onto side-channels
(``on_cli_activity`` / ``on_subagent_event``) via ``cli_demux.CliSubagentDemux``
so the TUI can render them as first-class cards instead of flattened text.

This module reuses the pure helpers in ``subagents.cli_backend`` (binary resolve,
argv build, ndjson reader) and only depends on ``pydantic_ai`` + ``..usage``, so
``config.build_model`` can import it lazily without a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..usage import COST_DETAIL_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pydantic_ai.messages import ModelMessage, ModelRequest
    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

# marim approval mode -> Claude Code --permission-mode. Headless `claude -p`
# cannot pop a per-tool prompt, so marim's "ask" (gate each mutation) has no
# faithful equivalent. Mapping it to "acceptEdits" would let the CLI edit files
# with NO gating while marim's UI still says "ask" — a silent authority
# escalation the user never consented to (the only disclosure was a
# once-per-process log line a TUI user never sees). So an unconsented "ask"
# degrades to the safe read-only "plan": the CLI can read/plan but not mutate,
# which is the conservative reading of "don't act without my approval". Only the
# explicit "auto" mode maps to acceptEdits. "ask" isn't listed, so it falls
# through to the ``.get(..., "plan")`` default alongside any unknown mode.
_MODE_MAP = {"auto": "acceptEdits", "plan": "plan"}


def permission_mode_for(mode: str) -> str:
    """The Claude ``--permission-mode`` for a marim approval mode."""
    return _MODE_MAP.get(mode, "plan")


def _part_text(content) -> str:
    """A UserPromptPart/TextPart content reduced to plain text. Content is a str
    or a list whose str items are joined (non-str multimodal items are skipped)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c for c in content if isinstance(c, str))
    return "" if content is None else str(content)


def latest_user_text(messages: list[ModelMessage]) -> str:
    """Text of the newest user prompt (what we send to ``claude -p`` each turn)."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    for msg in reversed(messages):
        if isinstance(msg, ModelRequest):
            texts = [
                _part_text(p.content) for p in msg.parts if isinstance(p, UserPromptPart)
            ]
            if texts:
                return "\n".join(t for t in texts if t)
    return ""


def extract_system(messages: list[ModelMessage]) -> str:
    """The system text for ``--append-system-prompt``: the most recent request's
    ``instructions`` if present, else the concatenated SystemPromptPart content."""
    from pydantic_ai.messages import ModelRequest, SystemPromptPart

    for msg in reversed(messages):
        if isinstance(msg, ModelRequest) and getattr(msg, "instructions", None):
            return str(msg.instructions)
    sys_parts: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            sys_parts += [
                _part_text(p.content) for p in msg.parts if isinstance(p, SystemPromptPart)
            ]
    return "\n".join(s for s in sys_parts if s)


def _render_tool_args(args) -> str:
    """A tool call's args as a compact one-line string. dicts are JSON-encoded so
    the keys/values survive; a raw-string args payload is passed through."""
    if isinstance(args, str):
        return args
    if args is None:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _request_lines(msg: ModelRequest) -> list[str]:
    """The rendered lines for one ``ModelRequest`` in :func:`flatten_history`:
    a user prompt's text and any tool-return results."""
    from pydantic_ai.messages import ToolReturnPart, UserPromptPart

    lines: list[str] = []
    for p in msg.parts:
        if isinstance(p, UserPromptPart):
            text = _part_text(p.content)
            if text:
                lines.append(f"User: {text}")
        elif isinstance(p, ToolReturnPart):
            lines.append(f"Tool {p.tool_name} returned: {_part_text(p.content)}")
    return lines


def _response_lines(msg: ModelResponse) -> list[str]:
    """The rendered lines for one ``ModelResponse`` in :func:`flatten_history`:
    assistant prose and any tool calls it made."""
    from pydantic_ai.messages import TextPart, ToolCallPart

    lines: list[str] = []
    for p in msg.parts:
        if isinstance(p, TextPart) and p.content:
            lines.append(f"Assistant: {p.content}")
        elif isinstance(p, ToolCallPart):
            lines.append(f"Assistant called {p.tool_name}({_render_tool_args(p.args)})")
    return lines


def flatten_history(messages: list[ModelMessage]) -> str:
    """The whole conversation rendered to one prompt, for a cold first turn (no
    Claude session to resume).

    claude-cli's own responses are text-only, but a cold start can also happen
    after switching providers mid-session — so the history may carry tool-call
    and tool-return parts produced by another provider using marim's tools. We
    render those too (``Assistant called <tool>(<args>)`` / ``Tool <tool>
    returned: <result>``) so the switched-in Claude sees what the tools did,
    not just the surrounding prose."""
    from pydantic_ai.messages import ModelRequest, ModelResponse

    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            lines.extend(_request_lines(msg))
        elif isinstance(msg, ModelResponse):
            lines.extend(_response_lines(msg))
    return "\n\n".join(lines)


def request_usage_from_cli(
    cli_usage: dict | None, total_cost_usd: float | None
) -> RequestUsage:
    """Build a ``RequestUsage`` from the CLI ``result`` event's usage block.

    Mirrors ``cli_backend.synth_usage`` (which returns a RunUsage for sub-agents):
    Anthropic reports ``input_tokens`` as the uncached bucket only, so we fold the
    cache read/write buckets back in to match the harness's cache-inclusive
    convention, and store the billed cost as integer micro-USD under
    ``details[COST_DETAIL_KEY]`` so the cost display needs no model-id lookup."""
    u = cli_usage or {}
    details: dict = {}
    if total_cost_usd is not None:
        details[COST_DETAIL_KEY] = int(total_cost_usd * 1_000_000)
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_creation_input_tokens", 0) or 0)
    uncached_in = int(u.get("input_tokens", 0) or 0)
    return RequestUsage(
        input_tokens=uncached_in + cache_read + cache_write,
        output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        details=details,
    )


# Claude tool_use -> the single arg worth showing on the activity line. Tools not
# listed render as the bare name. Mirrors the TUI's native label keys.
_ACTIVITY_ARG = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebSearch": "query",
    "WebFetch": "url",
    "Agent": "description",
    "Task": "description",
}


# A narrow, non-emoji marker. ``⏺`` (U+23FA) is emoji-presentation: terminals draw
# it 2 cells wide while rich/Textual lay it out as 1, so the line shifts and a
# character gets clipped at the wrap ("Read" rendered as "Rea"). ``▸`` is a plain
# geometric glyph that renders at its 1-cell width, keeping the layout honest.
_ACTIVITY_MARKER = "▸"


def format_activity_line(name: str, tool_input: dict) -> str:
    """A compact ``▸ <Tool> <summary>`` line for one Claude tool_use, folded into
    the streamed text so the user sees progress (we cannot surface real tool-call
    parts — pydantic_ai would try to execute them)."""
    key = _ACTIVITY_ARG.get(name)
    summary = ""
    if key:
        raw = tool_input.get(key, "")
        summary = " " + str(raw).strip().splitlines()[0] if str(raw).strip() else ""
    return f"{_ACTIVITY_MARKER} {name}{summary}"


@dataclass
class TextChunk:
    """A segment of assistant prose (one of Claude's text blocks, stripped)."""

    delta: str


@dataclass
class ToolUseChunk:
    """Claude invoked one of its own tools. Surfaced structurally so the TUI can
    render a native tool card; folded to a ``▸`` text line when there's no UI."""

    name: str  # Claude Code tool name, e.g. "Read"/"Bash"
    tool_input: dict
    call_id: str


@dataclass
class ToolResultChunk:
    """The result of a prior ``ToolUseChunk`` (matched by ``call_id``), so a live
    tool card can flip from pending to done/failed."""

    call_id: str
    content: str
    is_error: bool


@dataclass
class DoneChunk:
    """Terminal chunk: Claude's session id, usage, and whether a proper ``result``
    event was seen (``complete=False`` ⇒ crash/bad output). ``error_detail`` carries
    a tail of the CLI's stderr when the process failed, so the raised
    ``CliModelError`` can report *why* (a crash, a not-logged-in CLI, a bad flag)
    instead of a bare "no result"; it is ignored on a complete run."""

    session_id: str | None
    usage: RequestUsage
    complete: bool
    error_detail: str = ""


def _flatten_result_content(content) -> str:
    """A tool_result's content (str or list of content blocks) reduced to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return "" if content is None else str(content)


def _is_subagent_noise(obj: dict) -> bool:
    """True when ``obj`` is Claude-side sub-agent traffic — or that sub-agent's
    system-event lifecycle noise — neither of which belongs in the main turn.

    Objects tagged ``parent_tool_use_id`` belong to a Claude-side sub-agent, not
    the main turn; headless there is no demux to route them to, so they are
    dropped — otherwise a child's prose would leak into the main response text.
    ``task_started``/``task_updated``/``task_notification`` system events are the
    same sub-agent's lifecycle noise (the demux path renders it) and are skipped
    too."""
    if obj.get("parent_tool_use_id"):
        return True
    return obj.get("type") == "system" and obj.get("subtype") in (
        "task_started",
        "task_updated",
        "task_notification",
    )


def _assistant_chunks(obj: dict) -> Iterator:
    """The ``TextChunk``/``ToolUseChunk``s in one ``assistant`` stream-json
    object's content blocks."""
    for block in (obj.get("message") or {}).get("content") or []:
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text", "") or "").strip()
            if text:
                yield TextChunk(text)
        elif btype == "tool_use":
            yield ToolUseChunk(
                name=block.get("name", "tool"),
                tool_input=block.get("input") or {},
                call_id=block.get("id", ""),
            )


def _user_chunks(obj: dict) -> Iterator:
    """The ``ToolResultChunk``s in one ``user`` stream-json object's content
    blocks."""
    for block in (obj.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_result":
            yield ToolResultChunk(
                call_id=block.get("tool_use_id", ""),
                content=_flatten_result_content(block.get("content")),
                is_error=bool(block.get("is_error")),
            )


def _stream_chunks(kind: str, obj: dict) -> Iterator:
    """Dispatch one ``assistant``/``user`` stream-json object to
    :func:`_assistant_chunks`/:func:`_user_chunks`. A plain (sync) generator —
    unlike ``consume_cli_stream`` it CAN ``yield from``, so the caller collapses
    what would otherwise be two ``elif``/``for`` pairs into one."""
    if kind == "assistant":
        yield from _assistant_chunks(obj)
    elif kind == "user":
        yield from _user_chunks(obj)


def _merge_session_id(session_id: str | None, obj: dict) -> str | None:
    """The first-seen session id: keep the existing one, else take ``obj``'s."""
    return session_id or obj.get("session_id")


def _result_done_chunk(
    results: list[dict], obj: dict, session_id: str | None
) -> tuple[str | None, DoneChunk]:
    """Fold one ``result`` stream-json object into ``results`` and build the
    ``DoneChunk`` for it (usage folded across every result seen so far via
    ``sum_result_usages``, cost = the last result's cumulative total).

    Do NOT return early from the caller after this: an async sub-agent's
    completion re-invokes the main agent, so more turns (and another result)
    may follow. Consumers keep the LAST ``DoneChunk``."""
    from ..subagents.cli_backend import sum_result_usages

    session_id = _merge_session_id(session_id, obj)
    results.append(obj)
    summed, _turns, cost = sum_result_usages(results)
    return session_id, DoneChunk(
        session_id=session_id,
        usage=request_usage_from_cli(summed, cost),
        complete=True,
    )


async def consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncIterator:
    """Turn parsed stream-json objects into structured chunks then one or more
    ``DoneChunk``s.

    Assistant ``text`` blocks become ``TextChunk``s; ``tool_use`` blocks become
    ``ToolUseChunk``s; ``tool_result`` blocks (in ``user`` messages) become
    ``ToolResultChunk``s. Keeping tool activity structured (rather than pre-folded
    into text) lets the TUI render native tool cards, while the headless paths fold
    it back to ``▸`` lines via ``fold_chunk_text``.

    Objects tagged ``parent_tool_use_id`` belong to a Claude-side sub-agent, not
    the main turn; headless there is no demux to route them to, so they are
    dropped here — otherwise a child's prose would leak into the main response
    text. ``task_started``/``task_updated``/``task_notification`` system events are
    the same sub-agent's lifecycle noise and are skipped too.

    A ``result`` event used to end the generator with ``return``. That's a bug:
    closing the generator runs ``spawn_cli_objects``'s ``finally``, which kills the
    CLI process — but `claude -p` can emit MULTIPLE ``result`` events in one
    process, because an async sub-agent's completion re-invokes the main agent for
    another turn, ending in another ``result``. Returning early killed the CLI
    while that sub-agent was still running. So each ``result`` now yields a
    ``DoneChunk`` (usage folded across every result seen so far via
    ``sum_result_usages``, cost = the last result's cumulative total) and the loop
    keeps reading to EOF; consumers keep the LAST ``DoneChunk`` (``request()``
    already ``continue``s past each one). A stream that ends without any ``result``
    yields a trailing ``DoneChunk(complete=False)``."""
    session_id: str | None = None
    results: list[dict] = []
    error_detail = ""
    async for obj in objs:
        if _is_subagent_noise(obj):
            # Sub-agent-internal traffic, or its lifecycle noise. With a UI the
            # demux tee (see ClaudeCliStreamedResponse) consumes the traffic
            # before we ever see it; headless it is dropped here so a child's
            # prose never pollutes the main response text.
            continue
        kind = obj.get("type")
        if kind == "__cli_error__":
            # Synthetic sentinel from spawn_cli_objects at EOF carrying a tail of
            # the CLI's stderr (and exit code) when the process failed. Not a real
            # stream event — captured here so a "no result" turn can explain itself.
            error_detail = str(obj.get("stderr") or "").strip() or error_detail
            continue
        if kind == "system":
            session_id = _merge_session_id(session_id, obj)
        elif kind in ("assistant", "user"):
            for chunk in _stream_chunks(kind, obj):
                yield chunk
        elif kind == "result":
            # Do NOT return early: an async sub-agent's completion re-invokes the
            # main agent, so more turns (and another result) may follow.
            # Consumers keep the LAST DoneChunk.
            session_id, done = _result_done_chunk(results, obj, session_id)
            yield done
    if not results:
        yield DoneChunk(
            session_id=session_id,
            usage=RequestUsage(),
            complete=False,
            error_detail=error_detail,
        )


def fold_chunk_text(chunk, *, leading: bool) -> str:
    """The text representation of a visible chunk for the headless (no-UI) paths:
    assistant prose as-is, a ``ToolUseChunk`` as its ``▸`` activity line. Segments
    are blank-line separated (``leading`` is True only for the first one).
    ``ToolResultChunk``/``DoneChunk`` contribute no text. Returns ``""`` to skip."""
    if isinstance(chunk, TextChunk):
        segment = chunk.delta
    elif isinstance(chunk, ToolUseChunk):
        segment = format_activity_line(chunk.name, chunk.tool_input)
    else:
        return ""
    return segment if leading else f"\n\n{segment}"


def cli_activity_events(chunk) -> list:
    """Translate a tool chunk into display-only pydantic-ai stream events for the
    TUI side-channel — a ``FunctionToolCallEvent`` for a use, a
    ``FunctionToolResultEvent`` for a result. Tool names/args are normalized to the
    harness shapes (Read→read_file, …) so they render as native tool cards. These
    NEVER enter the model response, so pydantic_ai never executes them."""
    from datetime import datetime, timezone

    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from ..subagents.cli_backend import normalize_cc_tool

    if isinstance(chunk, ToolUseChunk):
        name, args = normalize_cc_tool(chunk.name, chunk.tool_input)
        return [
            FunctionToolCallEvent(
                part=ToolCallPart(tool_name=name, args=args, tool_call_id=chunk.call_id)
            )
        ]
    if isinstance(chunk, ToolResultChunk):
        return [
            FunctionToolResultEvent(
                part=ToolReturnPart(
                    tool_name="tool",
                    content=chunk.content,
                    tool_call_id=chunk.call_id,
                    timestamp=datetime.now(tz=timezone.utc),
                    outcome="failed" if chunk.is_error else "success",
                )
            )
        ]
    return []


_ask_noticed = False


def note_ask_limitation_once(mode: str) -> None:
    """Warn once per process that ``ask`` can't do per-tool gating in this provider,
    so it runs read-only (``plan``) rather than silently escalating to unattended
    edits. Kept out of the pure mapping so tests stay quiet."""
    global _ask_noticed
    if mode == "ask" and not _ask_noticed:
        _ask_noticed = True
        logger.warning(
            "claude-cli provider: 'ask' mode cannot gate individual tools "
            "(headless claude can't prompt) — running read-only ('plan'). "
            "Switch to 'auto' to let this provider edit files."
        )


class CliModelError(Exception):
    """The claude CLI was unavailable or produced no terminal result."""


def _no_result_message(done: DoneChunk | None) -> str:
    """The error text for a turn that ended without a proper ``result`` event.
    Appends the CLI's stderr tail (captured on the terminal ``DoneChunk``) when
    present so a crash / not-logged-in / bad-flag failure carries a real
    diagnostic instead of a bare, undebuggable line."""
    base = "claude produced no result (crash or bad output)"
    detail = done.error_detail if done is not None else ""
    return f"{base}: {detail}" if detail else f"{base}."


# How much of a failing CLI's stderr to fold into the raised error. Enough to carry
# a real diagnostic (a stack tail, "Invalid API key", "not logged in") without
# dumping an unbounded log into the message.
_STDERR_TAIL_CHARS = 2000


async def spawn_cli_objects(argv: list[str], cwd: str) -> AsyncIterator[dict]:
    """Spawn ``claude`` and yield each stream-json line as a parsed dict. Reaps the
    child on exit; drains stderr to avoid a pipe-buffer deadlock. Non-JSON noise is
    skipped. This is the only I/O seam — tests replace it via ``model.spawn``.

    On a nonzero exit the drained stderr is not discarded: a tail of it is yielded
    as a synthetic ``{"type": "__cli_error__", ...}`` sentinel so ``consume_cli_stream``
    can attach it to the terminal ``DoneChunk`` and the "no result" ``CliModelError``
    can explain the failure (crash, bad flag, not logged in) instead of staying
    silent."""
    from ..subagents.cli_backend import _iter_ndjson_lines

    proc = await asyncio.create_subprocess_exec(  # pragma: no cover
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = (  # pragma: no cover
        asyncio.ensure_future(proc.stderr.read()) if proc.stderr is not None else None
    )
    try:  # pragma: no cover
        assert proc.stdout is not None
        async for raw in _iter_ndjson_lines(proc.stdout):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        stderr_bytes = b""
        if stderr_task is not None:
            stderr_bytes = await stderr_task
            stderr_task = None
        code = await proc.wait()
        if code not in (0, None):
            detail = stderr_bytes.decode("utf-8", "replace").strip()[-_STDERR_TAIL_CHARS:]
            yield {"type": "__cli_error__", "stderr": detail, "returncode": code}
    finally:  # pragma: no cover
        if stderr_task is not None:
            stderr_task.cancel()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await proc.wait()


class ClaudeCliModel(Model):
    """A Pydantic AI model backed by the ``claude`` CLI (a Claude subscription).

    Each request spawns ``claude -p`` (resuming Claude's session when one is known)
    and returns a single text-only ``ModelResponse``; Claude runs its own tools
    internally. ``mode_getter`` is set by bootstrap to read marim's live approval
    mode; ``session_id`` is held in-memory across turns of one process."""

    def __init__(self, model_id: str | None, *, ephemeral: bool = False) -> None:
        super().__init__()
        self._model_id = model_id
        self.mode_getter: Callable[[], str] | None = None
        self.session_id: str | None = None
        # Ephemeral models are for one-shot aux agents (titler/summarizer): they
        # never resume or store a session, so they can't continue — or hijack —
        # the user's live Claude session, and they always send their own system
        # prompt. See ``ephemeral_clone``.
        self.ephemeral = ephemeral
        # Late-bound by bind_ui (TUI only) to a coroutine that renders Claude's own
        # tool_use/tool_result as native tool cards in the main transcript. When None
        # (headless, or no UI) tool activity is folded into the text as ▸ lines.
        # Never enters the model response, so pydantic_ai never executes these calls.
        self.on_activity: Callable[[list], Awaitable[None]] | None = None
        # Late-bound by bind_ui (TUI only): routes a Claude-side sub-agent's
        # translated events to the sub-agents screen (on_subagent ≙
        # Deps.ui.on_subagent_event) and relabels its card with the model the
        # child reports (on_subagent_model). None headless — the stream filter
        # in consume_cli_stream then simply drops child traffic.
        self.on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
        self.on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None
        self.spawn = spawn_cli_objects  # I/O seam; tests monkeypatch this
        # Late-bound by bootstrap/set_model to marim's real workspace (or worktree)
        # root, exactly like ``mode_getter``. Spawning in the process cwd (".") would
        # make Claude read/edit the WRONG directory — destructively so under --worktree.
        self.cwd: str = "."
        self._ts = datetime.now(tz=timezone.utc)

    def ephemeral_clone(self, *, cwd: str) -> ClaudeCliModel:
        """A stateless, read-only copy for one-shot aux agents (titler/summarizer).

        It never resumes or stores a Claude session — so titling/summarizing can't
        continue or hijack the user's live conversation — always sends its own
        instructions, and runs in plan (read-only) mode so it can't edit files."""
        clone = ClaudeCliModel(self._model_id, ephemeral=True)
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        return clone

    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    @property
    def system(self) -> str:
        return "claude-cli"

    def _argv(self, messages: list) -> list[str]:
        from ..subagents.cli_backend import build_cli_argv as _build
        from ..subagents.cli_backend import resolve_cli_binary

        binary = resolve_cli_binary()
        if binary is None:
            raise CliModelError(
                "claude CLI not found (set MARIM_CLAUDE_CLI_BIN or install Claude Code)."
            )
        mode = self.mode_getter() if self.mode_getter is not None else "plan"
        note_ask_limitation_once(mode)
        if self.session_id and not self.ephemeral:
            prompt, append_system, resume = latest_user_text(messages), False, self.session_id
        else:
            # Cold turn: re-seed Claude with the whole conversation (resumed marim
            # session, first turn, or any ephemeral aux call). For a brand-new
            # session this is just the one user message. Ephemeral models always
            # take this path — never resuming the user's live session.
            prompt, append_system, resume = flatten_history(messages), True, None
        return _build(
            binary,
            prompt,
            extract_system(messages),
            permission_mode_for(mode),
            [],  # let Claude use its own native toolset for the permission mode
            self._model_id,
            resume_session_id=resume,
            append_system=append_system,
            # Isolate from the user's plugins/hooks (notably agentmemory's
            # cross-session context injection) so a marim turn isn't polluted by —
            # or recorded into — other Claude sessions' memory. Auth/model/tools
            # keep working.
            safe_mode=True,
        )

    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        argv = self._argv(messages)
        done: DoneChunk | None = None
        parts: list[str] = []  # assistant prose + folded ▸ tool lines (no UI here)
        async for chunk in consume_cli_stream(self.spawn(argv, self.cwd)):
            if isinstance(chunk, DoneChunk):
                done = chunk
                continue
            segment = fold_chunk_text(chunk, leading=not parts)
            if segment:
                parts.append(segment)
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        if done.session_id and not self.ephemeral:
            self.session_id = done.session_id
        return ModelResponse(
            parts=[TextPart(content="".join(parts))],
            model_name=self.model_name,
            timestamp=self._ts,
            usage=done.usage,
            provider_name="claude-cli",
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        argv = self._argv(messages)
        stream = ClaudeCliStreamedResponse(
            model_request_parameters=model_request_parameters,
            _objs=self.spawn(argv, self.cwd),
            _model_id=self.model_name,
            _ts=self._ts,
            _set_session=(
                None if self.ephemeral else lambda sid: setattr(self, "session_id", sid)
            ),
            _on_activity=self.on_activity,
            _on_subagent=self.on_subagent,
            _on_subagent_model=self.on_subagent_model,
        )
        yield stream


class _TextFolder:
    """The vendor-part-id bookkeeping for ``_get_event_iterator``'s two
    rendering modes.

    With a UI side-channel (cards mode, ``on_activity`` set), Claude's
    tool_use/tool_result become native tool cards pushed out-of-band, and each
    run of assistant prose gets its own text part (a fresh vendor_part_id after
    every tool) so the cards interleave between text blocks. Headless (fold
    mode, no side-channel) folds tool_use into the text as ``▸`` lines in one
    growing part. ``part_n`` (cards mode) and ``folded_any`` (fold mode, for
    blank-line separation) both mutate across chunks AND across the text/tool
    arms, so they're threaded via this small stateful object rather than loose
    locals passed in/out of each arm."""

    def __init__(
        self, parts_manager, on_activity: Callable[[list], Awaitable[None]] | None
    ) -> None:
        self._parts_manager = parts_manager
        self._on_activity = on_activity
        self._cards = on_activity is not None
        self.part_n = 0
        self.folded_any = False

    async def _emit(self, content: str, part_id: str):
        for event in self._parts_manager.handle_text_delta(
            vendor_part_id=part_id, content=content
        ):
            yield event

    async def emit_text(self, delta: str):
        """A ``TextChunk``: cards mode gives it its own vendor part id
        (``text-{part_n}``); fold mode grows the single ``text-0`` part,
        blank-line-separated from anything already folded into it."""
        if self._cards:
            async for ev in self._emit(delta, f"text-{self.part_n}"):
                yield ev
            return
        seg = delta if not self.folded_any else f"\n\n{delta}"
        async for ev in self._emit(seg, "text-0"):
            yield ev
        self.folded_any = True

    async def emit_tool(self, chunk):
        """A ``ToolUseChunk``/``ToolResultChunk``: cards mode pushes it
        out-of-band via ``on_activity`` and, for a ``ToolUseChunk``, bumps
        ``part_n`` so following prose starts a fresh part below the card;
        fold mode folds it into the text stream as a ``▸`` line."""
        if self._on_activity is not None:
            events = cli_activity_events(chunk)
            if events:
                await self._on_activity(events)
            if isinstance(chunk, ToolUseChunk):
                self.part_n += 1  # following prose starts a fresh part below the card
            return
        seg = fold_chunk_text(chunk, leading=not self.folded_any)
        if seg:
            async for ev in self._emit(seg, "text-0"):
                yield ev
            self.folded_any = True


@dataclass
class ClaudeCliStreamedResponse(StreamedResponse):
    """Streams ``consume_cli_stream`` output as text-delta events. Stores Claude's
    session id back on the model when the terminal chunk arrives."""

    _objs: AsyncIterator[dict] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _set_session: Callable[[str], None] | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None
    _on_subagent: Callable[[str, object, object], Awaitable[None]] | None = None
    _on_subagent_model: Callable[[str, str], Awaitable[None]] | None = None

    async def _demuxed_objs(self) -> AsyncIterator[dict]:
        """Tee the raw stream through a CliSubagentDemux: Claude-side sub-agent
        traffic is delivered out-of-band (the synthesized spawn_agent call/
        return via _on_activity — the top-level sink claims those and builds
        the live card — and child events via _on_subagent, keyed by the spawn's
        tool_use id); everything else flows on to the chunk pipeline."""
        from ..subagents.cli_demux import CliSubagentDemux

        demux = CliSubagentDemux()
        assert self._objs is not None
        async for obj in self._objs:
            routed, remainder = demux.route(obj)
            for r in routed:
                if r.stream_id is None:
                    if self._on_activity is not None:
                        await self._on_activity([r.event])
                elif self._on_subagent is not None:
                    if r.model and self._on_subagent_model is not None:
                        await self._on_subagent_model(r.stream_id, r.model)
                    await self._on_subagent(r.stream_id, r.event, r.usage)
            if remainder is not None:
                yield remainder

    def _finalize_done(self, done: DoneChunk | None) -> None:
        """Mirror ``request()``: a stream that ends without a proper ``result``
        event (Claude crashed / produced no result) is a FAILED turn — raise so
        the harness flushes its resumable baseline (clean failure).

        Called once, AFTER ``_get_event_iterator``'s loop — not applied as each
        DoneChunk arrives — because `claude -p` can emit several ``result``
        events in one process (an async sub-agent's completion re-invokes the
        main agent for another turn), and marking the stream finished on the
        first one would cut the run short. The last DoneChunk wins."""
        if done is None or not done.complete:
            raise CliModelError(_no_result_message(done))
        self._usage = done.usage
        if done.session_id and self._set_session is not None:
            self._set_session(done.session_id)
        self._finished = True

    async def _get_event_iterator(self):
        if self._objs is None:
            return
        # The demux tee is active only when the sub-agent side-channel is wired
        # (a UI is bound); headless keeps the cheap filter-only path in
        # consume_cli_stream (Claude-side child traffic is simply dropped there).
        objs = self._demuxed_objs() if self._on_subagent is not None else self._objs
        folder = _TextFolder(self._parts_manager, self._on_activity)

        done: DoneChunk | None = None
        async for chunk in consume_cli_stream(objs):
            if isinstance(chunk, TextChunk):
                async for ev in folder.emit_text(chunk.delta):
                    yield ev
            elif isinstance(chunk, (ToolUseChunk, ToolResultChunk)):
                async for ev in folder.emit_tool(chunk):
                    yield ev
            elif isinstance(chunk, DoneChunk):
                done = chunk  # last one wins (multi-result runs)
        self._finalize_done(done)

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    @property
    def provider_url(self) -> str:
        return "https://claude.com/claude-code"
