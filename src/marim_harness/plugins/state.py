"""The per-scope installed-plugin registry, stored as ``plugins.json`` inside
each scope's ``plugins/`` directory. A missing or malformed registry reads as
empty — never fatal."""

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import config_dir


@dataclass
class InstalledPlugin:
    """One registry entry: where the plugin came from and its current state."""

    name: str
    version: str | None
    source: dict
    enabled: bool = True
    trusted: bool = False
    linked: bool = False
    installed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "source": self.source,
            "enabled": self.enabled,
            "trusted": self.trusted,
            "linked": self.linked,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "InstalledPlugin":
        return cls(
            name=name,
            version=data.get("version"),
            source=data.get("source") or {},
            enabled=bool(data.get("enabled", True)),
            trusted=bool(data.get("trusted", False)),
            linked=bool(data.get("linked", False)),
            installed_at=str(data.get("installed_at", "") or ""),
        )


def global_plugins_dir() -> Path:
    """Global plugin cache + registry (``~/.config/marim/plugins/``)."""
    return config_dir() / "plugins"


def project_plugins_dir(workspace_root) -> Path:
    """Project plugin cache + registry (``<ws>/.marim/plugins/``)."""
    return Path(workspace_root) / ".marim" / "plugins"


def state_path(plugins_dir: Path) -> Path:
    return Path(plugins_dir) / "plugins.json"


def load_state(plugins_dir: Path) -> dict[str, "InstalledPlugin"]:
    path = state_path(plugins_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, InstalledPlugin] = {}
    for name, entry in plugins.items():
        if isinstance(entry, dict):
            out[name] = InstalledPlugin.from_dict(name, entry)
    return out


def save_state(plugins_dir: Path, state: dict[str, "InstalledPlugin"]) -> None:
    path = state_path(plugins_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plugins": {name: rec.to_dict() for name, rec in state.items()}}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


__all__ = [
    "InstalledPlugin",
    "global_plugins_dir",
    "project_plugins_dir",
    "state_path",
    "load_state",
    "save_state",
]
