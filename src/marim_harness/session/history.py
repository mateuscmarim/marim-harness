"""Part-position slicing across upstream message-envelope normalization."""

from dataclasses import replace

from pydantic_ai.messages import ModelMessage


def slice_message_parts(
    messages: list[ModelMessage], *, start: int = 0, stop: int | None = None
) -> list[ModelMessage]:
    """Slice recorded parts without changing their order or message metadata.

    Upstream may merge adjacent requests without changing their parts. Part
    positions therefore survive envelope normalization; message indices do not.
    Only cut envelopes are copied, and input messages are never mutated.
    """
    result = []
    position = 0
    for message in messages:
        end = position + len(message.parts)
        left = max(0, start - position)
        right = len(message.parts) if stop is None else min(len(message.parts), stop - position)
        if left < right:
            result.append(
                message
                if left == 0 and right == len(message.parts)
                else replace(message, parts=message.parts[left:right])
            )
        position = end
    return result
