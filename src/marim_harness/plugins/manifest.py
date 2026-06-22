"""Parse and validate a plugin's ``.marim-plugin/plugin.json`` manifest.

Strict (``load_manifest``) raises ``ManifestError`` on any problem — used at
install/validate time. Lenient (``try_load_manifest``) returns ``None`` and
logs — used at discovery time so a broken plugin never breaks a turn.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_DIR = ".marim-plugin"
MANIFEST_FILE = "plugin.json"

# Same identifier rule as skills/agents.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ManifestError(Exception):
    """A manifest is missing, unreadable, or invalid."""


def valid_plugin_name(name) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= 64
        and _NAME_RE.match(name) is not None
    )


@dataclass(frozen=True)
class PluginManifest:
    """A parsed manifest. ``root`` is the plugin's directory; ``raw`` is the
    decoded JSON object."""

    name: str
    root: Path
    raw: dict

    @property
    def version(self) -> str | None:
        v = self.raw.get("version")
        return str(v) if v is not None else None

    @property
    def description(self) -> str:
        return str(self.raw.get("description", "") or "")

    @property
    def author(self) -> dict:
        a = self.raw.get("author")
        return a if isinstance(a, dict) else {}

    @property
    def homepage(self) -> str | None:
        v = self.raw.get("homepage")
        return str(v) if v else None

    @property
    def repository(self) -> str | None:
        v = self.raw.get("repository")
        return str(v) if v else None

    @property
    def license(self) -> str | None:
        v = self.raw.get("license")
        return str(v) if v else None

    @property
    def keywords(self) -> list[str]:
        k = self.raw.get("keywords")
        return [str(x) for x in k] if isinstance(k, list) else []

    def _resolve(self, value, default: str) -> Path:
        rel = value if isinstance(value, str) and value.strip() else default
        root = self.root.resolve()
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise ManifestError(f"path escapes plugin root: {rel!r}")
        return target

    def skills_dir(self) -> Path:
        return self._resolve(self.raw.get("skills"), "skills")

    def agents_dir(self) -> Path:
        return self._resolve(self.raw.get("agents"), "agents")

    def instructions_path(self) -> Path:
        return self._resolve(None, "AGENTS.md")

    def hooks_source(self) -> Path | dict | None:
        v = self.raw.get("hooks")
        if isinstance(v, dict):
            return v
        return self._resolve(v, "hooks/hooks.json")

    def mcp_source(self) -> Path | dict | None:
        v = self.raw.get("mcpServers")
        if isinstance(v, dict):
            return v
        return self._resolve(v, "mcp.json")


def _read_raw(plugin_dir: Path) -> dict:
    path = Path(plugin_dir) / MANIFEST_DIR / MANIFEST_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"no readable manifest at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"manifest must be a JSON object: {path}")
    return data


def load_manifest(plugin_dir: Path) -> PluginManifest:
    """Strict parse. Raises ``ManifestError`` on any problem."""
    data = _read_raw(plugin_dir)
    name = data.get("name")
    if not valid_plugin_name(name):
        raise ManifestError(f"invalid or missing plugin name: {name!r}")
    manifest = PluginManifest(name=str(name), root=Path(plugin_dir), raw=data)
    # Surface path-traversal problems eagerly for configured component paths.
    for key, getter in (
        ("skills", manifest.skills_dir),
        ("agents", manifest.agents_dir),
        ("hooks", manifest.hooks_source),
        ("mcpServers", manifest.mcp_source),
    ):
        if key in data and not isinstance(data[key], dict):
            getter()
    return manifest


def try_load_manifest(plugin_dir: Path) -> PluginManifest | None:
    """Lenient parse. Returns ``None`` (and logs) on any problem."""
    try:
        return load_manifest(plugin_dir)
    except ManifestError as exc:
        logger.warning("skipping plugin at %s: %s", plugin_dir, exc)
        return None


def substitute_root(value, plugin_root: Path):
    """Recursively replace ``${MARIM_PLUGIN_ROOT}`` with ``plugin_root`` in
    strings inside ``value`` (str/list/dict pass-through for other types)."""
    token = "${MARIM_PLUGIN_ROOT}"
    root = str(plugin_root)
    if isinstance(value, str):
        return value.replace(token, root)
    if isinstance(value, list):
        return [substitute_root(v, plugin_root) for v in value]
    if isinstance(value, dict):
        return {k: substitute_root(v, plugin_root) for k, v in value.items()}
    return value
