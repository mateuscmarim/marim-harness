"""Binary-safe text rendering for tool-return content.

``read_file`` can return an image as a pydantic-ai ``BinaryContent`` inside a
``ToolReturnPart`` (see ``tools/fs_tools.py`` / the read-images spec). Several
places in the codebase turn tool-return content into text for a sink that is
NOT the TUI's rich renderer: PostToolUse hook payloads written to a subprocess's
stdin (``hooks/dispatch.py``), the headless/WebSocket JSON event stream
(``stream_events.py``), and the advisor-facing plain-text transcript
(``compaction.py``). A bare ``str(BinaryContent)`` or
``json.dumps(x, default=str)`` on one of those objects embeds the full base64
body — up to ~20MB for a large image — into whatever sink is consuming it.
This module is the single source of truth for the compact placeholder those
sinks (and the TUI's own ``tool_result_text``) render instead, so an image
looks the same everywhere and bytes never leak into a text channel.
"""

from pydantic_ai.messages import BinaryContent


def binary_placeholder(content: BinaryContent) -> str:
    """Compact placeholder for one ``BinaryContent`` value: media type + size in KB
    (rounded down, floored at 1 so a tiny/empty payload doesn't read as "0 KB")."""
    kb = max(1, len(content.data) // 1024)
    return f"[image {content.media_type}, {kb} KB]"


def has_binary_content(content: object) -> bool:
    """True when ``content`` is a ``BinaryContent``, or a list/tuple containing one.

    Callers use this to decide whether to route through :func:`render_binary_safe`
    at all — non-binary content should keep its existing text/JSON rendering
    untouched (compatibility surface for stream-json consumers etc.)."""
    if isinstance(content, BinaryContent):
        return True
    return isinstance(content, (list, tuple)) and any(
        isinstance(item, BinaryContent) for item in content
    )


def render_binary_safe(content: object) -> str:
    """Render tool-return ``content`` as text, with any ``BinaryContent`` swapped
    for :func:`binary_placeholder` so raw bytes never reach a text sink.

    A scalar ``BinaryContent`` becomes the placeholder outright; a list/tuple is
    rendered item-by-item (binary items placeholder, everything else ``str()``)
    and space-joined; anything else falls back to plain ``str()``.
    """
    if isinstance(content, BinaryContent):
        return binary_placeholder(content)
    if isinstance(content, (list, tuple)):
        return " ".join(
            binary_placeholder(item) if isinstance(item, BinaryContent) else str(item)
            for item in content
        )
    return str(content)
