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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.usage import RequestUsage

from ..usage import COST_DETAIL_KEY

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

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
