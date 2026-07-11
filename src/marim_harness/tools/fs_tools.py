from pathlib import Path
from typing import Literal

from pydantic_ai import ModelRetry, RunContext

from ..runtime.deps import Deps
from ..workspace.skills import discover_skills
from .impl import fs


def _scratch_roots(ctx: RunContext[Deps]) -> tuple[Path, ...]:
    """The session scratchpad as an extra guard root, or () when unavailable.
    The live getter is called per tool call (not captured at registration) so
    the path tracks session switches; any failure inside it already degraded
    to None (see workspace/scratchpad.py), so this never raises."""
    getter = ctx.deps.services.get_scratchpad
    if getter is None:
        return ()
    p = getter()
    return (p,) if p is not None else ()


def read_file(
    ctx: RunContext[Deps], path: str, offset: int = 1, limit: int | None = None
) -> str:
    """Read a text file. `path` is relative to the workspace root.

    For large files, read a window instead of the whole thing: `offset` is the
    1-based line to start at and `limit` caps the line count. Prefer locating
    what you need first (with `grep`/`tree`) and reading a targeted range — a
    read with no `limit` is capped and will tell you how to page on.

    Skill directories (which may live outside the workspace) are also readable by
    their absolute path, so a skill's bundled files can be read this way too.
    Files in the session scratchpad directory are likewise readable by absolute
    path."""
    # Whitelist every discovered skill's directory for reading, so an agent that
    # reaches for a skill's bundled file by absolute path succeeds even when the
    # skill lives outside the workspace (discover_skills is cached per workspace).
    skills = discover_skills(ctx.deps.workspace.root, dirs=ctx.deps.workspace.skill_dirs)
    skill_roots = tuple(s.root for s in skills)
    return fs.read_file(
        ctx.deps.workspace.root, path, offset=offset, limit=limit,
        extra_read_roots=skill_roots + _scratch_roots(ctx), ledger=ctx.deps.reads,
    )


def glob(ctx: RunContext[Deps], pattern: str) -> str:
    """List files matching a glob pattern (e.g. `**/*.py`)."""
    return fs.glob_files(ctx.deps.workspace.root, pattern)


def tree(ctx: RunContext[Deps], path: str = ".", depth: int = 2) -> str:
    """Show a directory tree. `depth=1` lists one level (like ls); higher
    descends further. Noise dirs (.git, node_modules, …) aren't expanded."""
    return fs.tree(ctx.deps.workspace.root, path, depth)


def _grep_int_flag(key: str, val: object) -> int:
    """Coerce a ripgrep context flag (`-A`/`-B`/`-C`) value to a non-negative int,
    raising a model-facing retry on garbage rather than a 500."""
    try:
        return max(0, int(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ModelRetry(f"grep: {key} expects an integer, got {val!r}.") from None


def grep(
    ctx: RunContext[Deps],
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    type: str | None = None,
    output_mode: Literal["content", "files_with_matches", "count"] = "content",
    head_limit: int | None = None,
    multiline: bool = False,
    **flags: object,
) -> str:
    """Search file contents for a regex (ripgrep-style), returning matches in the
    workspace.

    - `path` scopes the search to a file or directory (default: whole workspace).
    - `glob` filters files by name, e.g. `*.py` or `*.{ts,tsx}`.
    - `type` filters by language, e.g. `py`, `js`, `rust`.
    - `output_mode`: `content` (default) shows `path:line:text`;
      `files_with_matches` lists only matching file paths; `count` shows
      `path:count` per file.
    - `head_limit` caps how many output rows come back.
    - `multiline` lets the pattern span lines (`.` matches newlines).
    - `-i` (bool) searches case-insensitively. `-n` is accepted but a no-op:
      line numbers are always included in `content` mode.
    - `-A` / `-B` / `-C` (ints) show that many context lines after / before /
      around each match (`content` mode only).

    Skips noise dirs (.git, node_modules, .venv, …) and binary files; large
    results are offloaded to a file with a preview."""
    case_insensitive = False
    before = after = 0
    for key, val in flags.items():
        if key == "-i":
            case_insensitive = bool(val)
        elif key == "-n":
            pass  # line numbers are always emitted in content mode
        elif key == "-A":
            after = _grep_int_flag(key, val)
        elif key == "-B":
            before = _grep_int_flag(key, val)
        elif key == "-C":
            before = after = _grep_int_flag(key, val)
        else:
            raise ModelRetry(
                f"grep: unknown argument {key!r}. Supported: pattern, path, glob, "
                "type, output_mode, head_limit, multiline, -i, -n, -A, -B, -C."
            )
    return fs.grep(
        ctx.deps.workspace.root,
        pattern,
        path,
        glob=glob,
        file_type=type,
        output_mode=output_mode,
        head_limit=head_limit,
        case_insensitive=case_insensitive,
        before_context=before,
        after_context=after,
        multiline=multiline,
    )
