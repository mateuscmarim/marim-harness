"""Import a Claude Code CLI memory store into marim's memory.

Claude Code keeps memory per *project directory*, outside the repo, under
``<claude-config>/projects/<cwd-slug>/memory/``. The on-disk shape there is the
one :mod:`marim_harness.workspace.memory` deliberately mirrors — a ``MEMORY.md``
index of one-line pointers plus one ``<slug>.md`` per fact — so this module is a
format *bridge*, not a translation.

The split follows the house convention: everything above ``read_source`` is
pure (path math, frontmatter parsing, conflict planning) and unit-tested
directly; ``read_source`` and ``apply_plan`` are the only functions that touch
disk, and ``apply_plan`` delegates every write to ``memory.save_memory`` so the
memory format keeps exactly one writer.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_CLAUDE_DIRNAME = ".claude"

# Claude's project-dir naming: every path separator and every dot becomes a
# dash. Both characters share one rule, which is why `/home/x/.local` yields the
# doubled `-home-x--local` seen on disk (one dash for the `/`, one for the `.`).
_SLUG_CHARS_RE = re.compile(r"[/.]")


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's config root: ``$CLAUDE_CONFIG_DIR`` when set to a non-blank
    value, else ``~/.claude``. ``env`` defaults to the live environment and is
    injectable so the pure path helpers stay testable without monkeypatching."""
    env = os.environ if env is None else env
    raw = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / _DEFAULT_CLAUDE_DIRNAME


def claude_project_slug(path: Path | str) -> str:
    """The directory name Claude Code uses for ``path``'s project.

    The path is resolved first (absolute, ``..`` collapsed, symlinks followed)
    so a relative or messy workspace argument lands on the same slug the Claude
    CLI would have produced from its own cwd.
    """
    resolved = Path(path).expanduser().resolve()
    return _SLUG_CHARS_RE.sub("-", str(resolved))


def claude_memory_dir(workspace: Path | str, *, config_dir: Path) -> Path:
    """Where Claude Code keeps the memory store for ``workspace``. The directory
    is not guaranteed to exist — callers check and fall back to listing."""
    return Path(config_dir) / "projects" / claude_project_slug(workspace) / "memory"
