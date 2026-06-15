from pathlib import Path
from typing import Optional

_PROJECT_INSTRUCTIONS_FILE = "AGENTS.md"


def load_project_instructions(
    workspace_root, filename: str = _PROJECT_INSTRUCTIONS_FILE
) -> Optional[str]:
    """Read project-specific agent instructions from ``filename`` in the
    workspace root. Returns the stripped text, or ``None`` if the file is
    absent, empty, or unreadable — a broken file must never break a turn."""
    path = Path(workspace_root) / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None
