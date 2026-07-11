"""Agent Skills support, following the agentskills.io open standard.

A skill is a *directory* whose name is its identity, containing a ``SKILL.md``
(YAML frontmatter + markdown body) and optionally bundling ``scripts/``,
``references/``, and ``assets/``. marim discovers skills from two roots in
precedence order — project before global —
injects a one-line ``name — description`` index into the prompt each turn, and
loads full bodies and bundled files on demand (the standard's progressive
disclosure). Scripts run through the ordinary ``bash`` tool using the absolute
path surfaced on activation, so they inherit its approval gating.

Nothing here raises into a turn: a malformed skill (no ``SKILL.md``, bad
frontmatter, missing description, name/dir mismatch, illegal name) is skipped,
never fatal.
"""

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import builtin_root, config_dir
from ._discovery import cached_discover
from ._frontmatter import FRONTMATTER_RE
from .fs import WorkspaceError, resolve_in_workspace
from .identifiers import valid_name

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"

# Truthy spellings for MARIM_TRUST_PROJECT_HOOKS, mirroring config.model._TRUTHY.
_TRUTHY = {"1", "true", "on", "yes"}


def _project_trusted(trust_project: bool | None) -> bool:
    """Whether project-local ``.marim/skills`` may load. Project-local skill
    descriptions are injected into the system prompt every turn and their bodies
    on activation, so a *cloned untrusted repo* could prompt-inject the main
    agent before any consent — the same threat the hooks/MCP subsystems gate.

    An explicit caller decision (threaded from ``cfg.trust_project_hooks``) wins.
    Absent one, we fall back to the *same signal* those subsystems use — the
    ``MARIM_TRUST_PROJECT_HOOKS`` env var — read here rather than threaded so the
    gate holds for the several un-wired call sites (instructions/provider/tui)
    without a functional regression for trusted repos. This is safe by default:
    a project's own ``.env`` is forbidden from setting that key (see
    config/env._PROJECT_ENV_BLOCKLIST), so a cloned repo cannot self-trust —
    the value comes only from the real shell env or the user's global config."""
    if trust_project is not None:
        return trust_project
    return os.getenv("MARIM_TRUST_PROJECT_HOOKS", "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class Skill:
    """One discovered skill: its identity, where it lives, and its metadata.
    ``root`` is the skill's own (absolute) directory; ``source`` names the
    discovery root it came from (e.g. ``project`` or ``global``). ``plugin`` is
    the owning plugin's name when the skill came from a plugin, else None."""

    name: str
    description: str
    root: Path
    source: str
    disable_model_invocation: bool = False
    allowed_tools: str = ""  # parsed but not enforced in v1
    metadata: dict = field(default_factory=dict)
    plugin: str | None = None

    @property
    def qualified_name(self) -> str:
        """The name used for display and lookup: ``plugin:name`` for plugin
        skills, the bare name otherwise."""
        return f"{self.plugin}:{self.name}" if self.plugin else self.name


def skill_roots(workspace_root) -> list[tuple[str, Path]]:
    """The discovery roots, highest precedence first: project, then global, then
    marim's bundled built-in skills."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
        ("builtin", builtin_root() / "skills"),
    ]


def _parse_skill(source: str, directory: Path, plugin: str | None = None) -> Skill | None:
    """Build a Skill from a directory, or None if it isn't a valid skill. The
    directory name is the authoritative identity; frontmatter must carry a
    non-empty description and, if it names the skill, must match the directory."""
    name = directory.name
    if not valid_name(name):
        return None
    skill_md = directory / _SKILL_FILE
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    fm_name = data.get("name")
    if fm_name is not None and str(fm_name) != name:
        return None
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    metadata = data.get("metadata")
    return Skill(
        name=name,
        description=description.strip(),
        root=directory.resolve(),
        source=source,
        disable_model_invocation=bool(data.get("disable-model-invocation", False)),
        allowed_tools=str(data.get("allowed-tools", "") or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
        plugin=plugin,
    )


def _all_skill_roots(
    workspace_root, *, trust_project: bool
) -> list[tuple[str, Path, str | None]]:
    """Discovery roots in precedence order as ``(source, root, plugin)``: user
    roots (project, then global) first, then plugin roots as ``plugin:name``.

    The project root is dropped unless ``trust_project`` — global skills always
    load; only the untrusted ``.marim/skills`` is gated. The same flag is
    threaded into ``plugin_skill_roots``, which gates *project-scope* plugin
    skills identically: a committed ``.marim/plugins/`` bundle is the same
    prompt-injection channel as a committed ``.marim/skills/``, so the two
    gates must not diverge (global-scope plugin skills always load). Because
    the cache signature is computed over this list, dropping a root also changes
    the fingerprint, so trusted and untrusted callers can't poison one cache."""
    from ..plugins import plugin_skill_roots

    roots: list[tuple[str, Path, str | None]] = [
        (source, root, None)
        for source, root in skill_roots(workspace_root)
        if source != "project" or trust_project
    ]
    roots += [
        (f"plugin:{name}", root, name)
        for name, root in plugin_skill_roots(workspace_root, trust_project=trust_project)
    ]
    return roots


# Cache of discovery results keyed by (resolved) workspace root, each tagged with
# the filesystem signature it was computed from. Mirrors the agents cache: skills
# are re-walked and re-parsed only when a SKILL.md is added/removed/edited, so the
# per-turn _skill_index instruction doesn't re-read and re-parse YAML every turn.
_DISCOVERY_CACHE: dict[str, tuple[tuple, list[Skill]]] = {}


def _discovery_signature(roots: list[tuple[str, Path, str | None]]) -> tuple:
    """A cheap stat-only fingerprint of every candidate skill: each skill dir's
    name plus its SKILL.md mtime and size. Changes whenever a skill is added,
    removed, or its SKILL.md edited — so a cache hit skips the read+YAML parse."""
    sig: list = []
    for source, root, _plugin in roots:
        try:
            dirs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            sig.append((source, str(root), None))
            continue
        files = []
        for d in dirs:
            try:
                st = (d / _SKILL_FILE).stat()
            except OSError:
                continue
            files.append((d.name, st.st_mtime_ns, st.st_size))
        sig.append((source, str(root), tuple(files)))
    return tuple(sig)


def discover_skills(
    workspace_root, *, trust_project: bool | None = None,
    dirs: "Sequence[Path] | None" = None,
) -> list[Skill]:
    """All effective skills for a workspace, deduped by qualified name with the
    first root in precedence order winning, sorted for stable display. User
    roots (bare names) come first, then plugin roots (``plugin:name``), so a
    user's own skill always beats a plugin's same-named one.

    ``trust_project`` gates the project-local ``.marim/skills`` root (see
    ``_project_trusted``); ``None`` defaults to the safe, env-derived signal.

    ``dirs``, when given (an embedder's ``WorkspaceConfig.skill_dirs``), replaces
    discovery entirely: only those directories are scanned, in the given order,
    and the project/global/plugin roots above are not consulted at all. This is
    the point, not an accident — an embedder that sets ``dirs`` is opting out of
    marim's own skill locations in favor of its own.

    Cached per workspace root and reused while the SKILL.md files on disk are
    unchanged (by name/mtime/size), so repeated calls within a turn — and across
    turns that didn't touch a skill — don't re-walk and re-parse them."""
    if dirs is not None:
        roots: list[tuple[str, Path, str | None]] = [
            ("explicit", Path(d), None) for d in dirs
        ]
    else:
        roots = _all_skill_roots(workspace_root, trust_project=_project_trusted(trust_project))
    return cached_discover(
        workspace_root, roots,
        _discovery_signature,
        _collect_skills,
        lambda s: s.qualified_name,
        _DISCOVERY_CACHE,
    )


def _collect_skills(seen: dict, source: str, root: Path, plugin: str | None) -> None:
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for directory in entries:
        skill = _parse_skill(source, directory, plugin=plugin)
        if skill is None:
            continue
        if skill.qualified_name in seen:
            continue  # a higher-precedence root already claimed this name
        seen[skill.qualified_name] = skill


def find_skill(
    workspace_root, name: str, *, trust_project: bool | None = None,
    dirs: "Sequence[Path] | None" = None,
) -> Skill | None:
    """The effective skill whose qualified name is ``name``, or None. Honors the
    same project-trust gate as ``discover_skills`` so an untrusted project's
    skill can't be activated by name either. ``dirs`` passes through to
    ``discover_skills`` unchanged (see there)."""
    for skill in discover_skills(workspace_root, trust_project=trust_project, dirs=dirs):
        if skill.qualified_name == name:
            return skill
    return None


def read_skill_body(skill: Skill) -> str:
    """The full ``SKILL.md`` text (frontmatter included), or a notice if it's
    gone (e.g. deleted between discovery and read)."""
    try:
        return (skill.root / _SKILL_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("failed to read skill %s: %s", skill.name, exc)
        return f"Skill {skill.name!r} has no readable {_SKILL_FILE}."


def read_bundled_file(skill: Skill, relpath: str) -> str:
    """Read a file bundled inside ``skill``, guarded against escaping the skill
    directory. Returns a notice (never raises) on a bad path or read error."""
    try:
        path = resolve_in_workspace(skill.root, relpath)
    except WorkspaceError:
        return f"path outside skill {skill.name!r}: {relpath}"
    if not path.is_file():
        return f"not a file: {relpath}"
    try:
        return path.read_text(errors="replace")
    except OSError as exc:
        logger.debug("failed to read bundled file %s in skill %s: %s", relpath, skill.name, exc)
        return f"could not read {relpath}: {exc}"


def skills_index_text(skills: list[Skill]) -> str:
    """The injected discovery index: one ``- name — description`` line per
    model-invocable skill. Skills marked ``disable-model-invocation`` are left
    out so the agent won't auto-activate them. Empty string when none qualify."""
    lines = [
        f"- {s.qualified_name} — {s.description}"
        for s in skills
        if not s.disable_model_invocation
    ]
    return "\n".join(lines)
