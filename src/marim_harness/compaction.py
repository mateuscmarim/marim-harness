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

import logging
from typing import Any, Awaitable, Callable, Optional

from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

logger = logging.getLogger(__name__)

_CHARS_PER_TOKEN = 4
# Rough flat cost for a binary attachment (image). A vision model tokenizes an
# image by its pixels/tiles, NOT by its base64 size, so counting the raw bytes
# as text would wildly overcount (a ~500KB screenshot is ~1-2k image tokens, not
# ~500k). This nominal value keeps the context gauge and compaction planning sane.
_IMAGE_TOKEN_ESTIMATE = 1500

Summarizer = Callable[[list[Any]], Awaitable[str]]


def estimate_tokens(history: list) -> int:
    """Rough token estimate (~4 chars/token) over the serialized part content.

    Binary attachments (images) are counted as a flat nominal cost rather than by
    their base64 length, which would massively overcount — image bytes are not
    tokenized as text by vision models."""
    chars = 0
    images = 0
    for message in history:
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, (list, tuple)):
                for item in content:
                    if isinstance(item, BinaryContent):
                        images += 1
                    elif item is not None:
                        chars += len(str(item))
            elif content is not None:
                chars += len(str(content))
            args = getattr(part, "args", None)
            if args is not None:
                chars += len(str(args))
    return chars // _CHARS_PER_TOKEN + images * _IMAGE_TOKEN_ESTIMATE


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


def will_compact(
    history: list,
    max_tokens: int,
    keep_last_messages: int = 20,
) -> bool:
    """Whether compacting ``history`` would actually drop anything — the same
    decision ``compact_history``/``compact_history_with_summary`` make, exposed
    so a caller can act *before* the (possibly expensive) compaction runs, e.g.
    firing a pre-compaction hook while the transcript is still full."""
    return _plan_tail_start(history, max_tokens, keep_last_messages) is not None


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


# Marks the synthetic message that replaces a compacted middle. The TUI keys off
# this prefix to render the summary as a distinct block instead of a user message.
SUMMARY_PREFIX = "[Summary of earlier conversation, condensed to save context]"


def summary_text(content) -> Optional[str]:
    """Return the summary body if ``content`` is a compaction summary message
    (a ``str`` starting with :data:`SUMMARY_PREFIX` followed by a non-empty body),
    else ``None``. The single source of truth for detecting/parsing a summary."""
    if not isinstance(content, str) or not content.startswith(SUMMARY_PREFIX):
        return None
    body = content[len(SUMMARY_PREFIX):].strip()
    return body or None


def _summary_message(summary: str) -> ModelRequest:
    return ModelRequest(
        parts=[UserPromptPart(content=f"{SUMMARY_PREFIX}\n\n{summary}")]
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
    except Exception as exc:
        logger.warning("compaction summarizer failed, falling back to truncation: %s", exc)
        summary = None

    if summary:
        return history[:1] + [_summary_message(summary)] + history[start:], True
    return history[:1] + history[start:], True


Titler = Callable[[list[Any]], Awaitable[str]]

_SUMMARY_INSTRUCTIONS = (
    "You compress a coding-session transcript into a dense summary so the agent "
    "can keep working with less context. Preserve: the user's goals and "
    "constraints, decisions made, files read or edited and what changed, command "
    "results, and any unresolved problems or next steps. Drop pleasantries and "
    "redundant detail. Write terse notes, not prose."
)

_TITLE_INSTRUCTIONS = (
    "You write a short, specific title for a coding session from its transcript. "
    "Reply with the title only — no quotes, no trailing punctuation, at most six "
    "words. Name the concrete task, e.g. 'Fix the parser off-by-one' or 'Add "
    "session auto-naming'."
)

_MAX_TITLE_CHARS = 50


def make_summarizer(model) -> Summarizer:
    """Build a summarizer backed by a dedicated, tool-free agent on ``model``."""
    summary_agent = Agent(model, instructions=_SUMMARY_INSTRUCTIONS)

    async def summarize(messages: list) -> str:
        result = await summary_agent.run(render_transcript(messages))
        return result.output

    return summarize


def clean_title(raw: str) -> str:
    """Reduce a model's reply to a single tidy title line, with a safe fallback."""
    lines = [line.strip() for line in (raw or "").splitlines()]
    text = next((line for line in lines if line), "")
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
    text = text.strip("\"'`").strip().rstrip(".!?,;:").strip()
    if len(text) > _MAX_TITLE_CHARS:
        text = text[:_MAX_TITLE_CHARS].rstrip() + "…"
    return text or "Untitled session"


def make_titler(model) -> Titler:
    """Build a titler backed by a dedicated, tool-free agent on ``model``."""
    title_agent = Agent(model, instructions=_TITLE_INSTRUCTIONS)

    async def title(messages: list) -> str:
        result = await title_agent.run(render_transcript(messages))
        return clean_title(result.output)

    return title
