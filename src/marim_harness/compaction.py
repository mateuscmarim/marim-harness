"""Context compaction: keep a conversation under a token budget.

When a history grows past the budget we keep the head (the original task anchor)
and a recent tail, and condense the middle. The tail must always begin at a clean
user-turn boundary so we never orphan a tool return from its tool call, which the
chat APIs reject.

Two strategies share the same head/tail split:

- ``compact_history`` (Phase 1): drop the middle entirely.
- ``compact_history_with_summary`` (Phase 2): summarize the middle into one
  synthetic message, falling back to truncation if the summary call fails.
"""

from typing import Awaitable, Callable, Optional

from pydantic_ai.messages import (
    ModelRequest,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

_CHARS_PER_TOKEN = 4

Summarizer = Callable[[list], Awaitable[str]]


def estimate_tokens(history: list) -> int:
    """Rough token estimate (~4 chars/token) over the serialized part content."""
    chars = 0
    for message in history:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if content is not None:
                chars += len(str(content))
            args = getattr(part, "args", None)
            if args is not None:
                chars += len(str(args))
    return chars // _CHARS_PER_TOKEN


def _is_user_turn(message) -> bool:
    """True for a ModelRequest that opens a user turn (carries a UserPromptPart).

    Such a message is a safe tail boundary: it never holds a dangling tool return.
    """
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def _plan_tail_start(
    history: list, max_tokens: int, keep_last_messages: int
) -> Optional[int]:
    """Index where the kept tail should begin, or None if no compaction is needed.

    The tail always starts at a user-turn boundary so tool returns stay paired.
    """
    if estimate_tokens(history) <= max_tokens:
        return None

    user_turns = [i for i, m in enumerate(history) if _is_user_turn(m) and i > 0]
    if not user_turns:
        return None

    ideal_start = len(history) - keep_last_messages
    candidates = [i for i in user_turns if i <= ideal_start]
    start = candidates[-1] if candidates else user_turns[0]
    if start <= 1:
        # The tail would begin at index 1, i.e. nothing meaningful to drop.
        return None
    return start


def compact_history(
    history: list,
    max_tokens: int,
    keep_last_messages: int = 20,
) -> tuple[list, bool]:
    """Return (history, did_compact) by dropping the middle when over budget."""
    start = _plan_tail_start(history, max_tokens, keep_last_messages)
    if start is None:
        return history, False
    return history[:1] + history[start:], True


def render_transcript(messages: list, max_part_chars: int = 2000) -> str:
    """Flatten messages into a plain-text transcript for the summarizer.

    Rendering to text (rather than passing the slice as message history) sidesteps
    the tool-call/return pairing rules — the summarizer just reads prose.
    """
    lines: list[str] = []
    for message in messages:
        role = "User" if isinstance(message, ModelRequest) else "Assistant"
        for part in getattr(message, "parts", []):
            if isinstance(part, UserPromptPart):
                lines.append(f"{role}: {_clip(part.content, max_part_chars)}")
            elif isinstance(part, TextPart):
                if part.content:
                    lines.append(f"Assistant: {_clip(part.content, max_part_chars)}")
            elif isinstance(part, ThinkingPart):
                if part.content:
                    lines.append(
                        f"Assistant (thinking): {_clip(part.content, max_part_chars)}"
                    )
            elif isinstance(part, ToolCallPart):
                lines.append(
                    f"Assistant called {part.tool_name}"
                    f"({_clip(part.args, max_part_chars)})"
                )
            elif isinstance(part, ToolReturnPart):
                lines.append(
                    f"Tool {part.tool_name} returned: "
                    f"{_clip(part.content, max_part_chars)}"
                )
    return "\n".join(lines)


def _clip(value, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _summary_message(summary: str) -> ModelRequest:
    return ModelRequest(
        parts=[
            UserPromptPart(
                content=(
                    "[Summary of earlier conversation, condensed to save context]\n\n"
                    f"{summary}"
                )
            )
        ]
    )


async def compact_history_with_summary(
    history: list,
    max_tokens: int,
    summarizer: Summarizer,
    keep_last_messages: int = 20,
) -> tuple[list, bool]:
    """Like ``compact_history`` but replace the dropped middle with a summary.

    Calls ``summarizer`` with the middle messages. If it raises or returns an
    empty string, falls back to plain truncation so a flaky summary model can
    never break a turn.
    """
    start = _plan_tail_start(history, max_tokens, keep_last_messages)
    if start is None:
        return history, False

    middle = history[1:start]
    summary: Optional[str]
    try:
        summary = await summarizer(middle)
    except Exception:
        summary = None

    if summary:
        return history[:1] + [_summary_message(summary)] + history[start:], True
    return history[:1] + history[start:], True
