"""Plugin system: installable bundles of skills, sub-agents, hooks, MCP
servers, and instructions, modeled on Claude Code's plugin format.

A plugin is a directory with a ``.marim-plugin/plugin.json`` manifest. It is
installed (copied or linked) into a global (``~/.config/marim/plugins/``) or
project (``<ws>/.marim/plugins/``) scope and tracked in a per-scope
``plugins.json`` registry. Discovery contributes a plugin's content into
marim's existing systems: skills/sub-agents/instructions for any *enabled*
plugin, hooks/MCP only for *enabled + trusted* ones.
"""

from .discovery import (
    ResolvedPlugin,
    discover_plugins,
    has_executable,
    plugin_agent_roots,
    plugin_bundle_summary,
    plugin_hook_entries,
    plugin_instruction_texts,
    plugin_mcp_specs,
    plugin_skill_roots,
)
from .manifest import (
    MANIFEST_DIR,
    MANIFEST_FILE,
    ManifestError,
    PluginManifest,
    load_manifest,
    substitute_root,
    try_load_manifest,
    valid_plugin_name,
)
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    save_state,
    state_path,
)

__all__ = [
    "MANIFEST_DIR",
    "MANIFEST_FILE",
    "ManifestError",
    "PluginManifest",
    "load_manifest",
    "substitute_root",
    "try_load_manifest",
    "valid_plugin_name",
    "InstalledPlugin",
    "global_plugins_dir",
    "load_state",
    "project_plugins_dir",
    "save_state",
    "state_path",
    "ResolvedPlugin",
    "discover_plugins",
    "has_executable",
    "plugin_agent_roots",
    "plugin_bundle_summary",
    "plugin_hook_entries",
    "plugin_instruction_texts",
    "plugin_mcp_specs",
    "plugin_skill_roots",
]
