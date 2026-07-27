"""Scan a workspace's *gated* project-local surface: everything that loads
only behind the project trust gate (see ``marim_harness.trust`` and
docs/guides/trust.md). Two consumers, one scan:

- the trust dialog / ``marim trust`` / ``GET .../trust`` list what a grant
  would enable, so the decision is informed rather than an opaque yes/no;
- stored decisions are keyed to ``fingerprint`` — canonical JSON over the
  *executable* surface only (hooks entries, MCP specs, project-plugin
  executable blocks). Inert content (skills/agents text) deliberately does
  NOT feed the fingerprint: editing a skill must not drop trust, the same
  policy the plugin registry applies (see plugins/install.py).

Read-only and tolerant: a missing or malformed config file reads as an empty
section, so any later real content registers as a fingerprint change."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .hooks.config import _read_hooks, project_hooks_config_path
from .mcp.config import _read_servers, project_mcp_config_path
from .plugins.install import plugin_surface_fingerprint


@dataclass(frozen=True)
class ProjectSurface:
    hook_events: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def empty(self) -> bool:
        return not (self.hook_events or self.mcp_servers or self.skills
                    or self.agents or self.plugins)

    def summary(self) -> str:
        """One line for the trust dialog / status readouts, naming counts and
        names so the user knows exactly what a grant enables."""
        parts = []
        if self.hook_events:
            parts.append(f"hooks: {len(self.hook_events)} ({', '.join(self.hook_events)})")
        if self.mcp_servers:
            parts.append(f"mcp: {len(self.mcp_servers)} ({', '.join(self.mcp_servers)})")
        if self.skills:
            parts.append(f"skills: {len(self.skills)}")
        if self.agents:
            parts.append(f"agents: {len(self.agents)}")
        if self.plugins:
            parts.append(f"plugins: {len(self.plugins)} ({', '.join(self.plugins)})")
        return " · ".join(parts) if parts else "none"


def _project_plugin_dirs(workspace_root: Path) -> list[Path]:
    """Project-scope plugin directories, sorted for a stable fingerprint.
    Non-directories (the registry file) are skipped."""
    root = Path(workspace_root) / ".marim" / "plugins"
    try:
        return sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def _skill_names(workspace_root: Path) -> list[str]:
    root = Path(workspace_root) / ".marim" / "skills"
    try:
        return sorted(d.name for d in root.iterdir()
                      if d.is_dir() and (d / "SKILL.md").is_file())
    except OSError:
        return []


def _agent_names(workspace_root: Path) -> list[str]:
    root = Path(workspace_root) / ".marim" / "agents"
    try:
        return sorted(p.stem for p in root.iterdir()
                      if p.is_file() and p.suffix == ".md")
    except OSError:
        return []


def scan_project_surface(workspace_root) -> ProjectSurface:
    ws = Path(workspace_root)
    hooks = _read_hooks(project_hooks_config_path(ws))
    servers = _read_servers(project_mcp_config_path(ws))
    plugin_dirs = _project_plugin_dirs(ws)
    fingerprint = json.dumps(
        {
            "hooks": hooks,
            "mcpServers": servers,
            "plugins": {p.name: plugin_surface_fingerprint(p) for p in plugin_dirs},
        },
        sort_keys=True, default=str,
    )
    return ProjectSurface(
        hook_events=sorted(hooks),
        mcp_servers=sorted(servers),
        skills=_skill_names(ws),
        agents=_agent_names(ws),
        plugins=[p.name for p in plugin_dirs],
        fingerprint=fingerprint,
    )
