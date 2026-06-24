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
    # Pin the memory tools' targets to the title/name: their args also carry a
    # multi-line `body`, so the order-dependent "first meaningful arg" fallback
    # could otherwise surface a chunk of memory text instead of the title.
    "remember": "title", "recall": "name",
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


def _task_field(task, key: str) -> str:
    """Read ``text``/``status`` from a task whether it arrived as a raw dict (the
    model's JSON) or a coerced Task object."""
    v = task.get(key, "") if isinstance(task, dict) else getattr(task, key, "")
    return " ".join(str(v).split())


def _task_digest(tasks) -> str:
    """A progress line for update_tasks: ``2/5 done · ▸ <current item>`` (the ▸
    matches the in-progress glyph in tasks.py). Empty when there are no items, so
    the header degrades to the bare label rather than dumping the raw list."""
    items = [t for t in (tasks or []) if _task_field(t, "text")]
    if not items:
        return ""
    done = sum(1 for t in items if _task_field(t, "status") == "done")
    current = next(
        (_task_field(t, "text") for t in items if _task_field(t, "status") == "in_progress"),
        "",
    )
    head = f"{done}/{len(items)} done"
    return f"{head} · ▸ {current}" if current else head


def _raw_target(tool_name: str, args: dict) -> str:
    if tool_name == "update_tasks":
        return _task_digest(args.get("todos"))
    if tool_name == "spawn_agent":
        # Prefer the short `description`, then the `task`. Pinning the target keeps
        # the order-dependent "first meaningful arg" fallback from surfacing a bare
        # `background: True` (which rendered as "Spawn Agent · True").
        v = args.get("description") or args.get("task") or ""
        return " ".join(str(v).split())
    key = _TARGET_ARG.get(tool_name)
    if key is not None:
        v = args.get(key)
        if v not in (None, "", [], {}):
            return " ".join(str(v).split())
    items = _meaningful(args)
    return " ".join(str(items[0]).split()) if items else ""


def _read_range(args: dict) -> str:
    """The ``:start-end`` suffix for a partial ``read_file`` (its ``offset``/
    ``limit`` window), or ``""`` for a full read (no offset, no limit).

    ``offset`` is the 1-based start line, ``limit`` the line count. An open-ended
    read (an offset but no limit, which pages to the cap) renders ``:start+`` —
    the end isn't known here. Matches the clickable ``path:line`` convention, so
    the start is a jump target."""
    offset = args.get("offset", 1)
    limit = args.get("limit")
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 1
    if offset < 1:
        offset = 1
    if limit in (None, ""):
        return f":{offset}+" if offset > 1 else ""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return f":{offset}+" if offset > 1 else ""
    if limit < 1:
        return f":{offset}+" if offset > 1 else ""
    return f":{offset}-{offset + limit - 1}"


def _badges(tool_name: str, args: dict) -> tuple[str, ...]:
    out: list[str] = []
    if tool_name == "bash" and args.get("background"):
        out.append("bg")
    if tool_name == "grep" and args.get("path"):
        out.append(f"in {args['path']}")
    # A global memory write/read hits the user-wide config dir, not this repo —
    # surface that, since project scope (the default) is the silent common case.
    if tool_name in ("remember", "recall") and args.get("scope") == "global":
        out.append("global")
    return tuple(out)


def summarize(tool_name: str, args: dict, *, cap: int = _PREVIEW_CAP) -> ToolSummary:
    raw = _raw_target(tool_name, args)
    clip = _clip_middle if tool_name == "bash" else _clip
    target = clip(raw, cap)
    # Append the read window after clipping the path, so a long path can't push
    # the range off the end — the range is the new, salient bit.
    if tool_name == "read_file" and target:
        target += _read_range(args)
    return ToolSummary(
        label=humanize_tool(tool_name),
        target=target,
        badges=_badges(tool_name, args),
    )
