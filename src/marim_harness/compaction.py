"""Context compaction: keep a conversation under a token budget.

Phase 1 is pure truncation — drop the middle of a long history while keeping the
head (the original task anchor) and a recent tail. The tail must always begin at
a clean user-turn boundary so we never orphan a tool return from its tool call,
which the chat APIs reject. Summarizing the dropped middle is a later phase that
will reuse this same skeleton.
"""

from pydantic_ai.messages import ModelRequest, UserPromptPart

_CHARS_PER_TOKEN = 4


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


def compact_history(
    history: list,
    max_tokens: int,
    keep_last_messages: int = 20,
) -> tuple[list, bool]:
    """Return (history, did_compact).

    If the estimate is within budget, return the input unchanged. Otherwise keep
    the first message plus the latest tail that starts at a user-turn boundary,
    targeting roughly the last ``keep_last_messages`` messages.
    """
    if estimate_tokens(history) <= max_tokens:
        return history, False

    user_turns = [i for i, m in enumerate(history) if _is_user_turn(m) and i > 0]
    if not user_turns:
        return history, False

    ideal_start = len(history) - keep_last_messages
    candidates = [i for i in user_turns if i <= ideal_start]
    start = candidates[-1] if candidates else user_turns[0]
    if start <= 1:
        # The tail would begin at index 1, i.e. nothing meaningful to drop.
        return history, False

    return history[:1] + history[start:], True
