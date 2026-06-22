"""Plugin system: installable bundles of skills, sub-agents, hooks, MCP
servers, and instructions, modeled on Claude Code's plugin format.

A plugin is a directory with a ``.marim-plugin/plugin.json`` manifest. It is
installed (copied or linked) into a global (``~/.config/marim/plugins/``) or
project (``<ws>/.marim/plugins/``) scope and tracked in a per-scope
``plugins.json`` registry. Discovery contributes a plugin's content into
marim's existing systems: skills/sub-agents/instructions for any *enabled*
plugin, hooks/MCP only for *enabled + trusted* ones.
"""

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

__all__ = [
    "MANIFEST_DIR",
    "MANIFEST_FILE",
    "ManifestError",
    "PluginManifest",
    "load_manifest",
    "substitute_root",
    "try_load_manifest",
    "valid_plugin_name",
]
