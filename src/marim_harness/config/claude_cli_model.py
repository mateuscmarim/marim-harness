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
