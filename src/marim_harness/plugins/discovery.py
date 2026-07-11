"""Resolve installed plugins and turn their bundled content into contributions
for marim's existing discovery systems.

Skills, sub-agents, and instructions are contributed for any *enabled* plugin
(inert text the model reads) — except that *project-scope* plugins also require
the project trust gate, since their registry travels with the repo (see
_enabled_inert). Hooks and MCP servers are contributed only for
*enabled + trusted* plugins, since they execute code. Project plugins shadow
global plugins of the same name."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .manifest import (
    MANIFEST_DIR,
    MANIFEST_FILE,
    PluginManifest,
    substitute_root,
    try_load_manifest,
)
from .state import (
    InstalledPlugin,
    global_plugins_dir,
    load_state,
    project_plugins_dir,
    state_path,
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


# --- stat-fingerprint discovery cache ---------------------------------------
#
# discover_plugins is on the per-turn hot path: skills and sub-agent discovery
# both call it (via plugin_skill_roots / plugin_agent_roots), and they do so
# *before* their own stat caches kick in, so without a cache here the registry
# and every plugin manifest are re-read and re-parsed twice per turn. We mirror
# the skills/agents stat-fingerprint cache (workspace/_discovery.py): a cheap
# stat-only signature of the two ``plugins.json`` registries plus every plugin's
# ``plugin.json`` manifest. A cache hit skips the json parses entirely; a miss
# (registry edited, plugin installed/removed/enabled, or a manifest changed)
# rebuilds. Keyed by resolved workspace root, with the scope dir paths folded
# into the signature so a changed config dir can't return another root's result.
_DISCOVERY_CACHE: dict[str, tuple[tuple, list[ResolvedPlugin]]] = {}


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _discovery_signature(scope_dirs: list[tuple[str, Path]]) -> tuple:
    """A stat-only fingerprint of both scopes: each scope's ``plugins.json`` and
    every present plugin's ``plugin.json`` (by name/mtime/size). Changes whenever
    the registry or any manifest is touched, so a cache hit skips the json
    parses. Stat-only by design — it deliberately does *not* read hooks/MCP
    sources, so the live-elevation trust guard must keep recomputing those (see
    _linked_elevation_revokes_trust)."""
    sig: list = []
    for scope, plugins_dir in scope_dirs:
        registry = _stat_key(state_path(plugins_dir))
        try:
            subdirs = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
        except OSError:
            subdirs = []
        manifests = []
        for d in subdirs:
            st = _stat_key(d / MANIFEST_DIR / MANIFEST_FILE)
            if st is not None:
                manifests.append((d.name, st))
        sig.append((scope, str(plugins_dir), registry, tuple(manifests)))
    return tuple(sig)


def discover_plugins(workspace_root) -> list[ResolvedPlugin]:
    """All installed plugins across both scopes (enabled and disabled), project
    shadowing global by name, sorted by name. Entries whose directory or
    manifest fails to load are skipped with a warning.

    Cached per workspace root and reused while the registries and manifests on
    disk are unchanged (by name/mtime/size), so the repeated per-turn calls from
    skills/agents discovery don't re-parse every ``plugins.json`` and
    ``plugin.json`` each time."""
    scope_dirs = _scope_dirs(workspace_root)
    sig = _discovery_signature(scope_dirs)
    key = str(Path(workspace_root).resolve())
    cached = _DISCOVERY_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    seen: dict[str, ResolvedPlugin] = {}
    for scope, plugins_dir in scope_dirs:
        for name, record in load_state(plugins_dir).items():
            if name in seen:
                continue
            # ``name`` is joined onto ``plugins_dir`` and its manifest is read, so
            # a traversal name would read manifests out of tree. load_state
            # guarantees every returned name is a valid kebab-case identifier, so
            # this join stays inside the scope dir.
            root = plugins_dir / name
            manifest = try_load_manifest(root)
            if manifest is None:
                logger.warning(
                    "plugin %r in registry (%s) has no loadable manifest at %s; skipping",
                    name, scope, root,
                )
                continue
            seen[name] = ResolvedPlugin(name, scope, root, record, manifest)
    result = sorted(seen.values(), key=lambda p: p.name)
    _DISCOVERY_CACHE[key] = (sig, result)
    return result


def _enabled(workspace_root) -> list[ResolvedPlugin]:
    return [p for p in discover_plugins(workspace_root) if p.enabled]


# Truthy spellings for MARIM_TRUST_PROJECT_HOOKS, mirroring config.model._TRUTHY.
_TRUTHY = {"1", "true", "on", "yes"}


def _project_trusted(trust_project: bool | None) -> bool:
    """Resolve the project-trust signal for the inert helpers. An explicit
    caller decision (threaded from ``cfg.trust_project_hooks``, e.g. via
    workspace skills/agents discovery) wins; absent one we fall back to the
    ``MARIM_TRUST_PROJECT_HOOKS`` env var — the same convention as
    ``workspace.skills._project_trusted``, and for the same reason: the gate
    must hold at un-wired call sites (the ``_plugin_instructions`` closure has
    no trust flag in reach) without regressing trusted repos. Safe by default:
    a project's own ``.env`` cannot set that key (config/env blocklists it), so
    a cloned repo cannot self-trust."""
    if trust_project is not None:
        return trust_project
    return os.getenv("MARIM_TRUST_PROJECT_HOOKS", "").strip().lower() in _TRUTHY


def _enabled_inert(workspace_root, trust_project: bool | None) -> list[ResolvedPlugin]:
    """Enabled plugins whose *inert* content (skills/agents/AGENTS.md) may be
    contributed.

    Project-scope plugins are dropped unless the project is trusted: their
    registry (``.marim/plugins/plugins.json``) travels with the repo, so on a
    fresh clone the ``enabled`` bit is whoever-committed-it-controlled, and the
    contributed text is injected into the model's context with no consent — the
    same prompt-injection channel the project's own ``.marim/skills`` /
    ``.marim/agents`` roots already gate behind ``MARIM_TRUST_PROJECT_HOOKS``
    (workspace/skills.py, workspace/agents.py); gating the byte-equivalent
    plugin content keeps the two consistent. Global-scope plugins were
    installed by an explicit user action into the user's own config dir,
    outside the repo's reach, and always contribute. The per-plugin ``trusted``
    bit is deliberately NOT required here — inert text doesn't execute code;
    the executable surface keeps its stricter gate in _enabled_trusted."""
    trusted = _project_trusted(trust_project)
    return [p for p in _enabled(workspace_root) if p.scope != "project" or trusted]


def _linked_elevation_revokes_trust(p: ResolvedPlugin) -> bool:
    """Whether a trusted *linked* plugin has gained executable surface (hooks/MCP)
    since trust was granted, so its executable contributions must NOT be honored.

    A linked plugin loads from a live, mutable source dir every discovery (unlike
    a copied/git install). install.py records ``executable_at_install`` on the
    source when trust is granted; the git-update path drops trust when an update
    introduces hooks/MCP, but a linked source can grow them silently with no such
    gate. We close that gap conservatively here: if the *live* manifest now ships
    executable parts that weren't present at trust time, treat it as untrusted for
    executable contributions (hooks/MCP) and warn loudly. Inert contributions
    (skills/agents/instructions) are unaffected — they don't execute code.

    The chosen behavior is "fail safe, don't auto-honor": rather than silently
    re-trusting newly-appeared executable surface, we withhold it until the user
    re-confirms trust (e.g. via a reinstall / explicit set_trusted). Only applies
    to linked plugins; copied/git installs are immutable on disk between updates."""
    if not p.record.linked:
        return False
    baseline = bool(p.record.source.get("executable_at_install"))
    if baseline:
        # Executable surface was already present and vetted at trust time; an
        # in-place edit to an *existing* hook command is a known residual risk of
        # linking a mutable source the user explicitly trusted, and is out of
        # scope for this presence-based guard.
        return False
    live = has_executable(plugin_bundle_summary(p.manifest))
    if live:
        logger.warning(
            "linked plugin %r gained executable surface (hooks/MCP) after it was "
            "trusted; refusing to auto-honor it. Re-confirm trust (reinstall or "
            "re-trust) to enable its hooks/MCP.",
            p.name,
        )
        return True
    return False


def _project_scope_untrusted(p: ResolvedPlugin, trust_project: bool) -> bool:
    """Whether a project-scope plugin's executable surface must be withheld
    because the *project* isn't trusted.

    A project plugin's registry (``.marim/plugins/plugins.json``) travels with
    the repo, so its ``trusted`` bit is whoever-committed-it-controlled — on a
    freshly cloned repo that's the exact supply-chain vector the
    ``MARIM_TRUST_PROJECT_HOOKS`` gate exists to close for ``.marim/hooks.json``
    and ``.marim/mcp.json``. Executable contributions (hooks/MCP) from project
    plugins therefore require *both* the per-plugin trust bit *and* the project
    trust gate. Global plugins are unaffected: they were installed by an explicit
    user action into the user's own config dir, outside the repo's reach. Inert
    contributions (skills/agents/instructions) are gated separately, in
    _enabled_inert — same project gate, but without requiring the per-plugin
    trust bit, since inert text doesn't execute code."""
    if p.scope != "project" or trust_project:
        return False
    if has_executable(plugin_bundle_summary(p.manifest)):
        logger.warning(
            "project plugin %r bundles hooks/MCP but the project is not trusted; "
            "withholding them. Set MARIM_TRUST_PROJECT_HOOKS=1 to honor project "
            "plugin hooks/MCP in this workspace.",
            p.name,
        )
    return True


def _enabled_trusted(workspace_root, *, trust_project: bool) -> list[ResolvedPlugin]:
    return [
        p for p in discover_plugins(workspace_root)
        if p.enabled and p.trusted
        and not _linked_elevation_revokes_trust(p)
        and not _project_scope_untrusted(p, trust_project)
    ]


def plugin_skill_roots(
    workspace_root, *, trust_project: bool | None = None
) -> list[tuple[str, Path]]:
    return [
        (p.name, p.manifest.skills_dir())
        for p in _enabled_inert(workspace_root, trust_project)
    ]


def plugin_agent_roots(
    workspace_root, *, trust_project: bool | None = None
) -> list[tuple[str, Path]]:
    return [
        (p.name, p.manifest.agents_dir())
        for p in _enabled_inert(workspace_root, trust_project)
    ]


# Keyed by resolved workspace root. The ``_global_instructions``/plugin closures
# call this on every model request; without a cache each enabled plugin's
# AGENTS.md is re-read per request. The signature covers the enabled set (a
# changed plugins.json reorders/resizes it via the now-cached discover_plugins)
# and each instructions file's stat, so an enable/disable or an AGENTS.md edit
# invalidates while an unchanged tree is served from cache.
_INSTRUCTION_TEXT_CACHE: dict[str, tuple[tuple, list[tuple[str, str]]]] = {}


def plugin_instruction_texts(
    workspace_root, *, trust_project: bool | None = None
) -> list[tuple[str, str]]:
    # Dropping a project-scope plugin under _enabled_inert's trust gate also
    # shrinks ``items`` — and with it the cache signature below — so trusted and
    # untrusted callers can't poison one cache entry for the other.
    items = [
        (p.name, p.manifest.instructions_path())
        for p in _enabled_inert(workspace_root, trust_project)
    ]
    sig = tuple((name, _stat_key(path)) for name, path in items)
    key = str(Path(workspace_root).resolve())
    cached = _INSTRUCTION_TEXT_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]

    out: list[tuple[str, str]] = []
    for name, path in items:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            out.append((name, text))
    _INSTRUCTION_TEXT_CACHE[key] = (sig, out)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_hooks_entries(source) -> dict | None:
    """Return the ``{event: [entries]}`` dict from a hooks source, or None."""
    if isinstance(source, dict):
        hooks = source.get("hooks") if "hooks" in source else source
    else:
        hooks = _read_json(source).get("hooks")
    return hooks if isinstance(hooks, dict) else None


def _resolve_mcp_servers(source) -> dict | None:
    """Return the ``{name: spec}`` dict from an MCP source, or None."""
    if isinstance(source, dict):
        servers = source.get("mcpServers") if "mcpServers" in source else source
    else:
        servers = _read_json(source).get("mcpServers")
    return servers if isinstance(servers, dict) else None


def plugin_hook_entries(workspace_root, *, trust_project: bool = False) -> dict:
    """Merged ``{event: [entry,...]}`` from enabled+trusted plugins, with
    ``${MARIM_PLUGIN_ROOT}`` substituted in each entry. Project-scope plugins
    contribute only when ``trust_project`` is set (their registry is committed
    to the repo, so the trust bit alone is not the user's word — see
    _project_scope_untrusted); the fail-safe default withholds them."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root, trust_project=trust_project):
        hooks = _resolve_hooks_entries(p.manifest.hooks_source())
        if hooks is None:
            continue
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            merged.setdefault(event, []).extend(
                substitute_root(e, p.root) for e in entries
            )
    return merged


def plugin_mcp_specs(workspace_root, *, trust_project: bool = False) -> dict:
    """Merged ``{namespaced_name: spec}`` from enabled+trusted plugins. Server
    names are namespaced ``<plugin>_<server>`` so two plugins never collide on
    a tool prefix. ``${MARIM_PLUGIN_ROOT}`` is substituted in each spec.
    Project-scope plugins contribute only when ``trust_project`` is set, same
    as plugin_hook_entries — MCP servers launch code on connect."""
    merged: dict = {}
    for p in _enabled_trusted(workspace_root, trust_project=trust_project):
        servers = _resolve_mcp_servers(p.manifest.mcp_source())
        if servers is None:
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
    hooks = _resolve_hooks_entries(manifest.hooks_source())
    if hooks is None:
        return 0
    return sum(len(v) for v in hooks.values() if isinstance(v, list))


def _count_mcp(manifest: PluginManifest) -> int:
    servers = _resolve_mcp_servers(manifest.mcp_source())
    return len(servers) if servers is not None else 0
