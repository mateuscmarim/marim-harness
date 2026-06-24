"""One shared tool-call summary used by every renderer (the main ``ToolCallWidget``
row, the ``ToolGroupWidget`` header, and the sub-agent card's ``↳`` line) so a call
reads the same everywhere: ``{Label} · {target}  {badges}``.

Each tool resolves to a humanized verb (``Read``/``Bash``/``Wait``), the *salient*
argument as the target (the command/path/pattern/id — picked per tool, not by
position or arg-count), and zero or more compact badges for the flags that would
otherwise be noise (``bg`` for a backgrounded bash; ``in <path>`` for a scoped
grep). Unknown tools fall back to a title-cased name + their first meaningful arg,
so nothing ever degrades to raw ``key=value`` repr."""

from dataclasses import dataclass

# Default cap for the main tool-row target; the sub-agent card passes a tighter cap.
_PREVIEW_CAP = 100

# Friendly verbs; unknown tools title-case their raw name (spawn_agent → "Spawn Agent").
_TOOL_LABELS = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit", "bash": "Bash",
    "grep": "Grep", "glob": "Glob", "tree": "Tree", "web_search": "Search",
    "fetch_url": "Fetch", "wait_for_job": "Wait", "spawn_agent": "Spawn Agent",
    "goto_definition": "Definition", "find_references": "References", "hover": "Hover",
    "document_symbols": "Symbols", "workspace_symbols": "Symbols",
    "diagnostics": "Diagnostics",
}

# The salient argument per tool — the one worth showing as the target. Tools absent
# here use the generic "first meaningful arg" fallback.
_TARGET_ARG = {
    "read_file": "path", "write_file": "path", "edit_file": "path",
    "bash": "command", "grep": "pattern", "glob": "pattern", "tree": "path",
    "wait_for_job": "id", "web_search": "query", "fetch_url": "url",
}


def humanize_tool(name: str) -> str:
    """A short, friendly verb for a tool call (``read_file`` → ``Read``)."""
    return _TOOL_LABELS.get(name) or name.replace("_", " ").title()


def _clip(text: str, limit: int = _PREVIEW_CAP) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip_middle(text: str, limit: int) -> str:
    """Clip from the middle, keeping head and tail — for shell pipelines whose tail
    (``… | tail -1``) is as informative as their head."""
    if len(text) <= limit:
        return text
    keep = limit - 1  # one char for the ellipsis
    tail = keep // 3
    head = keep - tail
    return text[:head] + "…" + (text[-tail:] if tail else "")


@dataclass(frozen=True)
class ToolSummary:
    label: str
    target: str
    badges: tuple[str, ...] = ()


def _meaningful(args: dict) -> list:
    return [v for v in args.values() if v not in (None, "", [], {})]


def _raw_target(tool_name: str, args: dict) -> str:
    key = _TARGET_ARG.get(tool_name)
    if key is not None:
        v = args.get(key)
        if v not in (None, "", [], {}):
            return " ".join(str(v).split())
    items = _meaningful(args)
    return " ".join(str(items[0]).split()) if items else ""


def _badges(tool_name: str, args: dict) -> tuple[str, ...]:
    out: list[str] = []
    if tool_name == "bash" and args.get("background"):
        out.append("bg")
    if tool_name == "grep" and args.get("path"):
        out.append(f"in {args['path']}")
    return tuple(out)


def summarize(tool_name: str, args: dict, *, cap: int = _PREVIEW_CAP) -> ToolSummary:
    raw = _raw_target(tool_name, args)
    clip = _clip_middle if tool_name == "bash" else _clip
    return ToolSummary(
        label=humanize_tool(tool_name),
        target=clip(raw, cap),
        badges=_badges(tool_name, args),
    )
