"""Resolve installed plugins and turn their bundled content into contributions
for marim's existing discovery systems.

Skills, sub-agents, and instructions are contributed for any *enabled* plugin
(inert text the model reads). Hooks and MCP servers are contributed only for
*enabled + trusted* plugins, since they execute code. Project plugins shadow
global plugins of the same name."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .manifest import PluginManifest, substitute_root, try_load_manifest
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedPlugin:
    """An installed plugin whose directory and manifest both loaded."""

    name: str
    scope: str  # "project" or "global"
    root: Path
    record: InstalledPlugin
    manifest: PluginManifest

    @property
    def enabled(self) -> bool:
        return self.record.enabled

    @property
    def trusted(self) -> bool:
        return self.record.trusted


def _scope_dirs(workspace_root) -> list[tuple[str, Path]]:
    # Highest precedence first: project shadows global.
    return [
        ("project", project_plugins_dir(workspace_root)),
        ("global", global_plugins_dir()),
    ]


def discover_plugins(workspace_root) -> list[ResolvedPlugin]:
    """All installed plugins across both scopes (enabled and disabled), project
    shadowing global by name, sorted by name. Entries whose directory or
    manifest fails to load are skipped with a warning."""
    seen: dict[str, ResolvedPlugin] = {}
    for scope, plugins_dir in _scope_dirs(workspace_root):
        for name, record in load_state(plugins_dir).items():
            if name in seen:
                continue
            root = plugins_dir / name
            manifest = try_load_manifest(root)
            if manifest is None:
                logger.warning(
                    "plugin %r in registry (%s) has no loadable manifest at %s; skipping",
                    name, scope, root,
                )
                continue
            seen[name] = ResolvedPlugin(name, scope, root, record, manifest)
    return sorted(seen.values(), key=lambda p: p.name)


def _enabled(workspace_root) -> list[ResolvedPlugin]:
    return [p for p in discover_plugins(workspace_root) if p.enabled]


def _enabled_trusted(workspace_root) -> list[ResolvedPlugin]:
    return [p for p in discover_plugins(workspace_root) if p.enabled and p.trusted]


def plugin_skill_roots(workspace_root) -> list[tuple[str, Path]]:
    return [(p.name, p.manifest.skills_dir()) for p in _enabled(workspace_root)]


def plugin_agent_roots(workspace_root) -> list[tuple[str, Path]]:
    return [(p.name, p.manifest.agents_dir()) for p in _enabled(workspace_root)]


def plugin_instruction_texts(workspace_root) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in _enabled(workspace_root):
        try:
            text = p.manifest.instructions_path().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            out.append((p.name, text))
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_hook_entries(workspace_root) -> dict:
    """Merged ``{event: [entry,...]}`` from enabled+trusted plugins, with
    ``${MARIM_PLUGIN_ROOT}`` substituted in each entry."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root):
        source = p.manifest.hooks_source()
        if isinstance(source, dict):
            hooks = source.get("hooks") if "hooks" in source else source
        else:
            hooks = _read_json(source).get("hooks")
        if not isinstance(hooks, dict):
            continue
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            merged.setdefault(event, []).extend(
                substitute_root(e, p.root) for e in entries
            )
    return merged


def plugin_mcp_specs(workspace_root) -> dict:
    """Merged ``{namespaced_name: spec}`` from enabled+trusted plugins. Server
    names are namespaced ``<plugin>_<server>`` so two plugins never collide on
    a tool prefix. ``${MARIM_PLUGIN_ROOT}`` is substituted in each spec."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root):
        source = p.manifest.mcp_source()
        if isinstance(source, dict):
            servers = source.get("mcpServers") if "mcpServers" in source else source
        else:
            servers = _read_json(source).get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for server_name, spec in servers.items():
            merged[f"{p.name}_{server_name}"] = substitute_root(spec, p.root)
    return merged


def plugin_bundle_summary(manifest: PluginManifest) -> dict:
    """Count what a plugin bundles, for the install-time trust prompt."""
    skills = _count_dirs_with(manifest.skills_dir(), "SKILL.md")
    agents = _count_files(manifest.agents_dir(), ".md")
    hooks = _count_hooks(manifest)
    mcp = _count_mcp(manifest)
    return {"skills": skills, "agents": agents, "hooks": hooks, "mcpServers": mcp}


def has_executable(summary: dict) -> bool:
    """Whether a bundle summary contains code-executing parts (hooks/MCP)."""
    return bool(summary.get("hooks")) or bool(summary.get("mcpServers"))


def _count_dirs_with(root: Path, marker: str) -> int:
    try:
        return sum(1 for d in root.iterdir() if d.is_dir() and (d / marker).is_file())
    except OSError:
        return 0


def _count_files(root: Path, suffix: str) -> int:
    try:
        return sum(1 for f in root.iterdir() if f.is_file() and f.suffix == suffix)
    except OSError:
        return 0


def _count_hooks(manifest: PluginManifest) -> int:
    source = manifest.hooks_source()
    if isinstance(source, dict):
        hooks = source.get("hooks", source)
    else:
        hooks = _read_json(source).get("hooks")
    if not isinstance(hooks, dict):
        return 0
    return sum(len(v) for v in hooks.values() if isinstance(v, list))


def _count_mcp(manifest: PluginManifest) -> int:
    source = manifest.mcp_source()
    if isinstance(source, dict):
        servers = source.get("mcpServers", source)
    else:
        servers = _read_json(source).get("mcpServers")
    return len(servers) if isinstance(servers, dict) else 0
