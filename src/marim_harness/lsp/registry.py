"""Map workspace files to multilspy language ids and report server availability.

Pure stdlib + small helpers, with no ``multilspy`` import, so importing the
registry (e.g. from the tools module) never drags in the heavy dependency or
spawns a language server.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

# File extension (lowercase, including dot) -> multilspy ``code_language``.
_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
}

# language -> (PATH probe binaries, install hint). A language with a non-empty
# probe tuple is "available" only when one of its binaries is on PATH. A language
# with an empty probe tuple is auto-provided by multilspy (it downloads the
# server on first use) and is always reported available.
_PROBES: dict[str, tuple[tuple[str, ...], str]] = {
    # multilspy starts jedi-language-server for Python (see multilspy's
    # LanguageServer.create), so probe for *that* binary — not pyright, which the
    # manager would never launch even when present.
    "python": (
        ("jedi-language-server",),
        "install jedi-language-server (pip install jedi-language-server)",
    ),
    "typescript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "javascript": (
        ("typescript-language-server",),
        "install typescript-language-server (npm i -g typescript-language-server typescript)",
    ),
    "cpp": (("clangd",), "install clangd (e.g. pacman -S clang)"),
    "java": ((), "auto-downloaded by multilspy on first use"),
}


def language_for(path: str) -> str | None:
    """Return the multilspy ``code_language`` for ``path``, or None if the file
    extension isn't one we support."""
    # Split the *basename* only: a dotted directory (e.g. ``src.v2/Makefile`` or
    # ``foo.bar/baz``) must not have its parent's dot mistaken for the file's
    # extension. ``splitext`` returns "" for an extensionless basename.
    _stem, ext = os.path.splitext(os.path.basename(path))
    if not ext:
        return None
    return _EXT_TO_LANG.get(ext.lower())


@dataclass(frozen=True)
class Availability:
    available: bool
    hint: str


def availability(language: str) -> Availability:
    """Whether a server for ``language`` can be started, with an install hint."""
    entry = _PROBES.get(language)
    if entry is None:
        return Availability(False, "unsupported language")
    probes, hint = entry
    if not probes:  # auto-provided by multilspy
        return Availability(True, hint)
    found = any(shutil.which(b) for b in probes)
    return Availability(found, hint)


# Directories pruned from the workspace-language scan: dependency/cache trees
# are large and say nothing about what the user edits (a .venv full of .py
# files must not report python for a pure-docs repo). Hidden directories are
# pruned wholesale, which also covers .git/.venv/.marim.
_SCAN_IGNORED_DIRS = frozenset({"node_modules", "__pycache__", "venv", "dist", "build", "target"})


def workspace_languages(root: str | os.PathLike, *, max_entries: int = 50_000) -> set[str]:
    """Languages present under ``root``, by file extension, from a bounded
    walk that prunes hidden and dependency/cache directories. Entries are
    visited in sorted order so the ``max_entries`` cap is deterministic.
    Best-effort by design: the cap keeps startup cheap on huge trees, so a
    language appearing only past it is simply not reported."""
    found: set[str] = set()
    seen = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _SCAN_IGNORED_DIRS
        )
        for name in sorted(filenames):
            seen += 1
            if seen > max_entries:
                return found
            language = language_for(name)
            if language is not None:
                found.add(language)
    return found


def locally_installed_languages() -> set[str]:
    """Languages whose server binary is on PATH right now. Excludes
    auto-download-only languages (e.g. java) so callers can cheaply start
    every locally-present server without triggering a multi-hundred-MB download."""
    out: set[str] = set()
    for language, (probes, _hint) in _PROBES.items():
        if probes and any(shutil.which(b) for b in probes):
            out.add(language)
    return out
