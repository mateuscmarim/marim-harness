"""Load and merge hook definitions from a global and an optional (trusted)
project config. Mirrors ``mcp/config.py``. The on-disk shape is Claude Code's:
a top-level ``hooks`` object mapping an event name to a list of entries."""

import json
from pathlib import Path

from ..config import config_dir


def global_hooks_config_path() -> Path:
    """The global hooks config, a sibling of the global ``.env``/``mcp.json``."""
    return config_dir() / "hooks.json"


def project_hooks_config_path(workspace_root: Path) -> Path:
    """The project-local hooks config, under the workspace's ``.marim/``."""
    return Path(workspace_root) / ".marim" / "hooks.json"


def _read_hooks(path: Path) -> dict:
    """Read the ``hooks`` mapping from a config file. A missing or malformed file
    yields ``{}`` — a broken config is skipped, never fatal."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def _merge_into(merged: dict, hooks: dict) -> None:
    """Merge event entries from ``hooks`` into ``merged``, concatenating per-event lists."""
    for event, entries in hooks.items():
        if isinstance(entries, list):
            merged.setdefault(event, []).extend(entries)


def load_hooks_config(workspace_root: Path, *, trust_project: bool) -> dict:
    """Merge global hook entries, project entries (only when ``trust_project``),
    and entries from enabled+trusted plugins into one ``{event: [entry, ...]}``
    map. Per-event lists are concatenated. Plugin hooks are gated by per-plugin
    trust; *project-scope* plugins additionally require ``trust_project``, since
    their registry (and its trust bit) is committed to the repo — the same gate
    that governs ``.marim/hooks.json``."""
    from ..plugins import plugin_hook_entries

    merged: dict = {}
    _merge_into(merged, _read_hooks(global_hooks_config_path()))
    if trust_project:
        _merge_into(merged, _read_hooks(project_hooks_config_path(workspace_root)))
    _merge_into(merged, plugin_hook_entries(workspace_root, trust_project=trust_project))
    return merged
