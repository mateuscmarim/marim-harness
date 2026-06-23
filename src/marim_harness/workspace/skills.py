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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import config_dir
from ..identifiers import valid_name
from ._frontmatter import FRONTMATTER_RE
from .fs import WorkspaceError, resolve_in_workspace

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"


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
    """The two discovery roots, highest precedence first: project over global."""
    ws = Path(workspace_root)
    return [
        ("project", ws / ".marim" / "skills"),
        ("global", config_dir() / "skills"),
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


def discover_skills(workspace_root) -> list[Skill]:
    """All effective skills for a workspace, deduped by qualified name with the
    first root in precedence order winning, sorted for stable display. User
    roots (bare names) come first, then plugin roots (``plugin:name``), so a
    user's own skill always beats a plugin's same-named one."""
    from ..plugins import plugin_skill_roots

    seen: dict[str, Skill] = {}
    for source, root in skill_roots(workspace_root):
        _collect_skills(seen, source, root, plugin=None)
    for plugin_name, root in plugin_skill_roots(workspace_root):
        _collect_skills(seen, f"plugin:{plugin_name}", root, plugin=plugin_name)
    return sorted(seen.values(), key=lambda s: s.qualified_name)


def _collect_skills(seen: dict, source: str, root: Path, *, plugin: str | None) -> None:
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


def find_skill(workspace_root, name: str) -> Skill | None:
    """The effective skill whose qualified name is ``name``, or None."""
    for skill in discover_skills(workspace_root):
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
