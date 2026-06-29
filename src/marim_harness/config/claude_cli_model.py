"""Run the Claude Code CLI (`claude -p`) as a main-loop model provider.

A Claude subscription is reachable only through the `claude` CLI, which runs its
own agentic loop — there is no raw per-step model endpoint behind it. So this
provider makes marim a *launcher*: ``ClaudeCliModel`` spawns ``claude -p`` in
stream-json mode, lets Claude run its own tools internally, and returns a single
**text-only** ``ModelResponse``. Emitting ``ToolCallPart``s here would make
pydantic_ai's agent graph try to execute Claude's tool calls a second time, so
Claude's internal tool activity is folded into the streamed text instead (see
``format_activity_line`` / ``consume_cli_stream``).

This module reuses the pure helpers in ``subagents.cli_backend`` (binary resolve,
argv build, ndjson reader) and only depends on ``pydantic_ai`` + ``..usage``, so
``config.build_model`` can import it lazily without a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..usage import COST_DETAIL_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

# marim approval mode -> Claude Code --permission-mode. Headless `claude -p`
# cannot pop a per-tool prompt, so marim's "ask" has no faithful equivalent; we
# treat it like "auto" (acceptEdits) and warn once (see note_ask_limitation_once).
# Anything unrecognized degrades to the safe read-only "plan".
_MODE_MAP = {"auto": "acceptEdits", "ask": "acceptEdits", "plan": "plan"}


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


def flatten_history(messages: list[ModelMessage]) -> str:
    """The whole conversation rendered to one prompt, for a cold first turn (no
    Claude session to resume). User turns and our prior text answers only —
    this model never produces tool-call parts, so there are none to render."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for p in msg.parts:
                if isinstance(p, UserPromptPart):
                    text = _part_text(p.content)
                    if text:
                        lines.append(f"User: {text}")
        elif isinstance(msg, ModelResponse):
            for p in msg.parts:
                if isinstance(p, TextPart) and p.content:
                    lines.append(f"Assistant: {p.content}")
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
}


def format_activity_line(name: str, tool_input: dict) -> str:
    """A compact ``⏺ <Tool> <summary>`` line for one Claude tool_use, folded into
    the streamed text so the user sees progress (we cannot surface real tool-call
    parts — pydantic_ai would try to execute them)."""
    key = _ACTIVITY_ARG.get(name)
    summary = ""
    if key:
        raw = tool_input.get(key, "")
        summary = " " + str(raw).strip().splitlines()[0] if str(raw).strip() else ""
    return f"⏺ {name}{summary}"


@dataclass
class TextChunk:
    """A piece of visible text (assistant prose or a folded activity line)."""

    delta: str


@dataclass
class DoneChunk:
    """Terminal chunk: the full text, Claude's session id, usage, and whether a
    proper ``result`` event was seen (``complete=False`` ⇒ crash/bad output)."""

    text: str
    session_id: str | None
    usage: RequestUsage
    complete: bool


async def consume_cli_stream(objs: AsyncIterator[dict]) -> AsyncIterator:
    """Turn parsed stream-json objects into ``TextChunk``s then one ``DoneChunk``.

    Assistant ``text`` blocks stream as-is; ``tool_use`` blocks are folded into the
    text as ``format_activity_line`` output. The terminal ``result`` event yields a
    ``DoneChunk`` carrying usage + session id. If the stream ends without a
    ``result``, the final ``DoneChunk`` has ``complete=False``."""
    text_parts: list[str] = []
    session_id: str | None = None
    async for obj in objs:
        kind = obj.get("type")
        if kind == "system":
            session_id = session_id or obj.get("session_id")
        elif kind == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    delta = block.get("text", "") or ""
                    if delta:
                        text_parts.append(delta)
                        yield TextChunk(delta)
                elif btype == "tool_use":
                    line = "\n" + format_activity_line(
                        block.get("name", "tool"), block.get("input") or {}
                    )
                    text_parts.append(line)
                    yield TextChunk(line)
        elif kind == "result":
            session_id = session_id or obj.get("session_id")
            yield DoneChunk(
                text="".join(text_parts),
                session_id=session_id,
                usage=request_usage_from_cli(obj.get("usage"), obj.get("total_cost_usd")),
                complete=True,
            )
            return
    yield DoneChunk(
        text="".join(text_parts), session_id=session_id, usage=RequestUsage(), complete=False
    )


_ask_noticed = False


def note_ask_limitation_once(mode: str) -> None:
    """Warn once per process that ``ask`` can't do per-tool gating in this provider
    (it is treated like ``auto``). Kept out of the pure mapping so tests stay quiet."""
    global _ask_noticed
    if mode == "ask" and not _ask_noticed:
        _ask_noticed = True
        logger.warning(
            "claude-cli provider: 'ask' mode cannot gate individual tools "
            "(headless claude can't prompt) — running like 'auto' (acceptEdits)."
        )


class CliModelError(Exception):
    """The claude CLI was unavailable or produced no terminal result."""


async def spawn_cli_objects(argv: list[str], cwd: str) -> AsyncIterator[dict]:
    """Spawn ``claude`` and yield each stream-json line as a parsed dict. Reaps the
    child on exit; drains stderr to avoid a pipe-buffer deadlock. Non-JSON noise is
    skipped. This is the only I/O seam — tests replace it via ``model.spawn``."""
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
        if stderr_task is not None:
            await stderr_task
            stderr_task = None
        await proc.wait()
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

    def __init__(self, model_id: str | None) -> None:
        super().__init__()
        self._model_id = model_id
        self.mode_getter: Callable[[], str] | None = None
        self.session_id: str | None = None
        self.spawn = spawn_cli_objects  # I/O seam; tests monkeypatch this
        self._ts = datetime.now(tz=timezone.utc)

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
        if self.session_id:
            prompt, append_system = latest_user_text(messages), False
        else:
            # Cold turn: re-seed Claude with the whole conversation (resumed marim
            # session or first turn). For a brand-new session this is just the one
            # user message.
            prompt, append_system = flatten_history(messages), True
        return _build(
            binary,
            prompt,
            extract_system(messages),
            permission_mode_for(mode),
            [],  # let Claude use its own native toolset for the permission mode
            self._model_id,
            resume_session_id=self.session_id,
            append_system=append_system,
        )

    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        argv = self._argv(messages)
        done: DoneChunk | None = None
        async for chunk in consume_cli_stream(self.spawn(argv, ".")):
            if isinstance(chunk, DoneChunk):
                done = chunk
        if done is None or not done.complete:
            raise CliModelError("claude produced no result (crash or bad output).")
        if done.session_id:
            self.session_id = done.session_id
        return ModelResponse(
            parts=[TextPart(content=done.text)],
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
            _objs=self.spawn(argv, "."),
            _model_id=self.model_name,
            _ts=self._ts,
            _set_session=lambda sid: setattr(self, "session_id", sid),
        )
        yield stream


@dataclass
class ClaudeCliStreamedResponse(StreamedResponse):
    """Streams ``consume_cli_stream`` output as text-delta events. Stores Claude's
    session id back on the model when the terminal chunk arrives."""

    _objs: AsyncIterator[dict] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _set_session: Callable[[str], None] | None = None

    async def _get_event_iterator(self):
        if self._objs is None:
            return
        async for chunk in consume_cli_stream(self._objs):
            if isinstance(chunk, TextChunk):
                for event in self._parts_manager.handle_text_delta(
                    vendor_part_id="content", content=chunk.delta
                ):
                    yield event
            elif isinstance(chunk, DoneChunk):
                self._usage = chunk.usage
                if chunk.session_id and self._set_session is not None:
                    self._set_session(chunk.session_id)
                self._finished = True

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
