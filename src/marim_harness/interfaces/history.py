"""Persistent prompt history: the lines the user has submitted at the prompt,
recalled shell-style with Up/Down. Stored as JSON Lines (one JSON-encoded string
per line) so multi-line prompts round-trip cleanly. The store is TUI-free and
testable on its own; the :class:`~marim_harness.tui.widgets.PromptInput` widget
navigates over ``entries`` and the app appends submissions via ``add``.
"""

import json
import logging
import os
from pathlib import Path

from ..atomic_io import atomic_write_text

logger = logging.getLogger(__name__)


def default_history_path() -> Path:
    """The global history file, a sibling of the sessions dir under the data
    home (``$XDG_DATA_HOME/marim-harness``, else ``~/.local/share/...``)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "marim-harness" / "prompt_history.jsonl"


class PromptHistory:
    """Submitted prompts, oldest first. Persists to ``path`` when given (created
    on first write); with ``path=None`` it stays purely in memory. Only the last
    ``max_entries`` are kept, both in memory and on disk."""

    def __init__(self, path: Path | None = None, max_entries: int = 1000) -> None:
        self.path = Path(path) if path is not None else None
        self.max_entries = max_entries
        self.entries: list[str] = self._load()

    def _load(self) -> list[str]:
        if self.path is None or not self.path.exists():
            return []
        entries: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("skipping corrupt line in %s", self.path)
                continue  # skip a corrupt line rather than lose the whole history
            if isinstance(value, str):
                entries.append(value)
        logger.debug("loaded %d prompt history entries from %s", len(entries), self.path)
        return entries[-self.max_entries:]

    def add(self, text: str) -> None:
        """Record a submitted prompt. Blanks and consecutive duplicates are
        dropped; the list is capped and (when persistent) written to disk."""
        text = text.strip()
        if not text:
            return
        if self.entries and self.entries[-1] == text:
            return
        self.entries.append(text)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(entry) for entry in self.entries)
        atomic_write_text(self.path, body + "\n" if body else "")
