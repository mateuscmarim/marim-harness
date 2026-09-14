"""Prompt assembly shared by external CLI providers.

A live provider already owns history, so only the newest user request goes
on the wire. A cold start replays the transcript with role labels, keeping
images next to the user text they accompanied. Text-only prompts retain the
original rendering, including whitespace, for compatibility and caching.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserContent,
    UserPromptPart,
)

from .external_cli import CliModelError


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
            texts = [_part_text(p.content) for p in msg.parts if isinstance(p, UserPromptPart)]
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


def _latest_request(messages: list[ModelMessage]) -> list[ModelMessage]:
    for msg in reversed(messages):
        if isinstance(msg, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in msg.parts
        ):
            return [msg]
    return []


def _user_content(messages: list[ModelMessage]) -> list[UserContent]:
    return [
        item
        for msg in messages
        if isinstance(msg, ModelRequest)
        for part in msg.parts
        if isinstance(part, UserPromptPart)
        for item in ([part.content] if isinstance(part.content, str) else part.content)
    ]


def _history_content(messages: list[ModelMessage]) -> list[UserContent]:
    content: list[UserContent] = []
    for msg in messages:
        if isinstance(msg, ModelResponse):
            content.append(flatten_history([msg]) + "\n\n")
            continue
        for part in msg.parts:
            if isinstance(part, UserPromptPart):
                content.append("User: ")
                content.extend([part.content] if isinstance(part.content, str) else part.content)
                content.append("\n\n")
            else:
                text = flatten_history([ModelRequest(parts=[part])])
                if text:
                    content.append(text + "\n\n")
    return content


def prompt_content(messages: list[ModelMessage], *, history: bool) -> list[UserContent]:
    """Preserve user attachments and their order, replaying history only on cold starts.

    Keep the text-only rendering byte-identical. A resumed CLI already owns
    earlier images; resending them wastes context and can change which image
    the user's follow-up refers to.
    """
    selected = messages if history else _latest_request(messages)
    content = _user_content(selected)
    if all(isinstance(item, str) for item in content):
        return [flatten_history(selected) if history else latest_user_text(selected)]
    return _history_content(selected) if history else content


def attachment_content(text: str, attachments: list[tuple[bytes, str]] | None) -> list[UserContent]:
    """The same binary content used by run_turn, for direct CLI steering."""
    return [text, *(BinaryContent(data=d, media_type=m) for d, m in attachments or [])]


def _image(item: UserContent) -> BinaryContent:
    if isinstance(item, BinaryContent) and item.is_image:
        return item
    raise CliModelError("CLI input supports text and binary image attachments only")


def claude_input(content: Sequence[UserContent]) -> list[dict]:
    """Claude stream-json user content; encode images inline without temp files."""
    blocks: list[dict] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "text", "text": item})
            continue
        image = _image(item)
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": image.base64,
                },
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def codex_input(content: Sequence[UserContent]) -> list[dict]:
    """Codex turn/start and turn/steer input; image URLs carry inline data URIs."""
    return [
        {"type": "text", "text": item, "text_elements": []}
        if isinstance(item, str)
        else {"type": "image", "url": _image(item).data_uri}
        for item in content
    ]
