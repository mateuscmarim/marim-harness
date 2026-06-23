"""Agent-managed task tracking: a TodoWrite-style checklist the model maintains
for the current multi-step job.

The model calls the ``update_tasks`` tool with the *whole* list each time, which
replaces the previous one — no id bookkeeping on the model's side. State lives on
:class:`~marim_harness.deps.Deps` as a :class:`TaskList` (the handle every tool
already receives), so the tool mutates it directly, the Harness persists it into
the session file, and the TUI refreshes a live panel through the ``on_change``
callback. Nothing here does I/O; persistence and rendering live with their owners.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

Status = Literal["pending", "in_progress", "done"]
_VALID: frozenset[str] = frozenset({"pending", "in_progress", "done"})
_SYMBOL = {"done": "✔", "in_progress": "▸", "pending": "○"}


@dataclass
class Task:
    """One checklist item. ``status`` defaults to ``pending`` so the model can add
    new items without specifying it."""

    text: str
    status: Status = "pending"


def _coerce(item) -> Task | None:
    """Turn a tool-supplied Task, a stored dict, or any text/status-bearing object
    into a valid Task — or None to drop it. Blank text is dropped; an unknown
    status is clamped to ``pending`` so bad input never breaks a turn."""
    if isinstance(item, Task):
        text, status = item.text, item.status
    elif isinstance(item, dict):
        text, status = item.get("text", ""), item.get("status", "pending")
    else:
        text = getattr(item, "text", "")
        status = getattr(item, "status", "pending")
    text = (text or "").strip()
    if not text:
        return None
    if status not in _VALID:
        status = "pending"
    return Task(text=text, status=cast(Status, status))


def _normalize(raw) -> list[Task]:
    return [task for task in (_coerce(item) for item in (raw or [])) if task is not None]


class TaskList:
    """The session's live checklist. Mutated in place across session switches so
    the TUI's reference and ``on_change`` wiring survive."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.items: list[Task] = []
        # Fired only on the live mid-turn path (replace). Lifecycle resets
        # (clear/load) are followed by a full re-render from the caller, so they
        # stay silent to avoid double-refreshing.
        self.on_change = on_change

    def replace(self, raw) -> None:
        """Swap in a new list from the model, validating each item, then notify."""
        self.items = _normalize(raw)
        if self.on_change is not None:
            self.on_change()

    def clear(self) -> None:
        """Empty the list silently (used by /clear and new-session)."""
        self.items = []

    def load(self, payload) -> None:
        """Restore from a persisted payload silently (used on resume/switch)."""
        self.items = _normalize(payload)

    def to_payload(self) -> list[dict]:
        """Serialize for the session file."""
        return [{"text": t.text, "status": t.status} for t in self.items]


def render_tasks(items: list[Task]) -> str:
    """The checklist body: one ``<symbol> <text>`` line per item, no header.
    Empty string when there are none."""
    return "\n".join(f"{_SYMBOL[t.status]} {t.text}" for t in items)


def summarize(items: list[Task]) -> str:
    """A one-line count by status, for the tool's return value."""
    if not items:
        return "no tasks"
    done = sum(1 for t in items if t.status == "done")
    in_progress = sum(1 for t in items if t.status == "in_progress")
    pending = sum(1 for t in items if t.status == "pending")
    return (
        f"{len(items)} tasks: {done} done, {in_progress} in progress, "
        f"{pending} pending"
    )
