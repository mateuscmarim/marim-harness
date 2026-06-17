"""Small persisted user preferences (currently just the chosen theme).

A single JSON file in the per-user config dir, mirroring the JSON-in-config_dir
pattern used by ``mcp.py``. Best-effort throughout: a missing or malformed file
never raises — it falls back to the default theme."""

import json
from pathlib import Path

from .config import config_dir
from .interfaces.tui.themes import DEFAULT_THEME, THEME_NAMES


def prefs_path() -> Path:
    """The prefs file: ``$XDG_CONFIG_HOME/marim/prefs.json`` (else under
    ``~/.config``)."""
    return config_dir() / "prefs.json"


def _read() -> dict:
    try:
        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_theme() -> str:
    """The saved theme name, or ``DEFAULT_THEME`` when absent/invalid/unknown."""
    name = _read().get("theme")
    return name if name in THEME_NAMES else DEFAULT_THEME


def save_theme(name: str) -> bool:
    """Persist ``name`` as the startup theme. Rejects unknown names (returns
    False, writes nothing). Best-effort: a write failure returns False rather
    than raising."""
    if name not in THEME_NAMES:
        return False
    data = _read()
    data["theme"] = name
    try:
        path = prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
